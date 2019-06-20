"""
These functions implement machine translation model training.
"""

from collections import OrderedDict
from fastai.basic_data import DataBunch
from fastai.callbacks.tracker import LearnerCallback, SaveModelCallback, TrackerCallback
from fastai.train import validate, Learner
import os
import torch
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel

from dataloader import PervasiveDataLoader
from pervasive import Pervasive


def check_params(params, param_list):
    """
    Checks that a list of parameters is found in the config file
    and throws an exception if not.
    """
    for param in param_list:
        try:
            val = params
            for key in param.split('.'):
                val = val[key]
        except (KeyError, TypeError):
            raise ValueError(f'Expected parameter "{param}" not supplied.')


def build_learner(params, project_dir, pindex=0, comm_file=None):
    """
    Builds a fastai `Learner` object containing the model and data specified by
    `params`. It is configured to run on GPU `device_id`. Assumes it is GPU
    `pindex` of `world_size` total GPUs. In case more than one GPU is being
    used, a file named `comm_file` is used to communicate between processes.
    """
    model_name = params['model_name']
    model_dir = os.path.join(project_dir, 'model', model_name)
    try:
        # Try to make the directory for saving models.
        os.makedirs(model_dir)
    except FileExistsError:
        pass

    # Configure GPU/CPU device settings.
    gpu_ids = params['gpu_ids']
    world_size = len(gpu_ids) if len(gpu_ids) > 0 else 1
    distributed = world_size > 1
    if gpu_ids:
        device_id = gpu_ids[pindex]
        device_name = torch.cuda.get_device_name(device_id)
        device = torch.device(device_id)
        torch.cuda.set_device(device_id)
    else:
        device_id = None
        device_name = 'cpu'
        device = torch.device('cpu')

    # If distributed, initialize inter-process communication using shared file.
    if distributed:
        torch.distributed.init_process_group(backend='nccl',
                                             world_size=world_size,
                                             rank=pindex,
                                             init_method=f'file://{comm_file}')

    # Load data.
    check_params(params, [
        'data.batch_size',
        'data.dir',
        'data.epoch_size',
        'data.max_length',
        'data.max_test_size',
        'data.max_val_size',
        'data.src',
        'data.tgt',
    ])
    batch_size = params['data']['batch_size'] // world_size
    data_dir = params['data']['dir']
    src_l = params['data']['src']
    tgt_l = params['data']['tgt']
    src_infos = os.path.join(data_dir, f'{src_l}.infos')
    tgt_infos = os.path.join(data_dir, f'{tgt_l}.infos')
    src_h5 = os.path.join(data_dir, f'{src_l}.h5')
    tgt_h5 = os.path.join(data_dir, f'{tgt_l}.h5')
    loader = PervasiveDataLoader(src_infos,
                                 src_h5,
                                 tgt_infos,
                                 tgt_h5,
                                 batch_size,
                                 params['data']['max_length'],
                                 model_name,
                                 epoch_size=params['data']['epoch_size'],
                                 max_val_size=params['data']['max_val_size'],
                                 max_test_size=params['data']['max_test_size'],
                                 distributed=distributed)
    # Define neural network.
    check_params(params, [
        'decoder.embedding_dim',
        'decoder.embedding_dropout',
        'decoder.prediction_dropout',
        'encoder.embedding_dim',
        'encoder.embedding_dropout',
        'network.bias',
        'network.block_sizes',
        'network.division_factor',
        'network.dropout',
        'network.efficient',
        'network.growth_rate',
    ])
    # Max length is 2 more than setting to account for BOS and EOS.
    model = Pervasive(
        model_name, loader.src_vocab, loader.tgt_vocab,
        params['network']['block_sizes'], params['data']['max_length'] + 2,
        params['data']['max_length'] + 2, params['encoder']['embedding_dim'],
        params['decoder']['embedding_dim'],
        params['encoder']['embedding_dropout'], params['network']['dropout'],
        params['decoder']['embedding_dropout'],
        params['decoder']['prediction_dropout'],
        params['network']['division_factor'], params['network']['growth_rate'],
        params['network']['bias'], params['network']['efficient'])

    model.init_weights()
    if device_id is not None:
        if not torch.cuda.is_available():
            raise ValueError(
                'Request to train on GPU {device_id}, but not GPU found.')
        model.cuda(device_id)
        if distributed:
            model = DistributedDataParallel(model, device_ids=[device_id])
    data = DataBunch(loader.loaders['train'],
                     loader.loaders['val'],
                     loader.loaders['test'],
                     device=device)
    return Learner(data, model, loss_func=F.cross_entropy, model_dir=model_dir)


def train_worker(pindex,
                 project_dir,
                 params,
                 comm_file=None,
                 checkpoint=None,
                 restore=None):
    """
    Trains the model as specified by `params` on GPU `gpu_ids[pindex]`.
    Uses `comm_file` to communicate between processes.
    Saves models and event logs to subdirectories of `project_dir`.

    This is run in separate processes from the command line app, with
    one process per GPU.

    Optionally save the model every `checkpoint` batches.

    Optionally load a saved model with filename `restore`.
    """
    # Variable used for distributed processing.
    if not os.getenv('RANK', None):
        os.environ['RANK'] = str(pindex)

    learn = build_learner(params, project_dir, pindex, comm_file)

    # Restore saved model if necessary.
    epoch = None
    if restore is not None:
        try:
            # Remove extension if provided to match `load()`'s expectation.
            if restore[-4:] == '.pth':
                restore = restore[:-4]
            learn.load(restore, purge=False)
            if pindex == 0:
                # Only print once even with multiple workers.
                print(f'Loaded model {restore}.')
        except FileNotFoundError:
            if pindex == 0:
                # Only print once even with mulitple workers.
                print(f'The model file {learn.model_dir}/{restore}.pth '
                      'was not found!')
            return
        fields = restore.split('/')[-1].split('_')
        if len(fields) > 1:
            try:
                epoch = int(fields[1]) + 1
            except:
                pass

    # Callbacks.
    learn.callbacks = [SaveModelCallback(learn, every='epoch', name='model')]

    # Train with a one cycle schedule for each epoch.
    check_params(params, [
        'optim.epochs',
        'optim.lr',
    ])
    learn.fit_one_cycle(params['optim']['epochs'],
                        params['optim']['lr'],
                        tot_epochs=params['optim']['epochs'],
                        start_epoch=epoch)