# ----------------------------------------------------------------------
# Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.
#
# This work is licensed under the NVIDIA Source Code License
# for Bongard-HOI. To view a copy of this license, see the LICENSE file.
# ----------------------------------------------------------------------

import logging
import os
import shutil
import time
import math
import warnings

import numpy as np
import torch
from torch import optim
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import SGD, Adam, AdamW
from torch.optim.lr_scheduler import MultiStepLR

from typing import Any
import sys
from . import few_shot

import glob
import PIL.Image
from torchvision import transforms
from torch.utils.data import DataLoader
import torch.distributed as dist
import torch.nn as nn
import functools
from copy import deepcopy

_log_path = None

_LOCAL_PROCESS_GROUP = None
"""
A torch process group which only includes processes that on the same machine as the current process.
This variable is set when processes are spawned by `launch()` in "engine/launch.py".
"""

import multiprocessing
from torch import Tensor
from typing import Optional, Iterable, Any, List, Union, Tuple


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)


def div(numerator: Tensor, denom: Union[Tensor, int, float]) -> Tensor:
    """Handle division by zero"""
    if type(denom) in [int, float]:
        if denom == 0:
            return torch.zeros_like(numerator)
        else:
            return numerator / denom
    elif type(denom) is Tensor:
        zero_idx = torch.nonzero(denom == 0).squeeze(1)
        denom[zero_idx] += 1e-8
        return numerator / denom
    else:
        raise TypeError("Unsupported data type ", type(denom))


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def set_log_path(path):
    global _log_path
    _log_path = path


def log(obj, filename='log.txt'):
    print(obj)
    if _log_path is not None:
        with open(os.path.join(_log_path, filename), 'a') as f:
            print(obj, file=f)


class Averager():

    def __init__(self):
        self.n = 0.0
        self.v = 0.0

    def add(self, v, n=1.0):
        self.v = (self.v * self.n + v * n) / (self.n + n)
        self.n += n

    def item(self):
        return self.v


class Timer():

    def __init__(self):
        self.v = time.time()

    def s(self):
        self.v = time.time()

    def t(self):
        return time.time() - self.v


def set_gpu(gpu):
    print('set gpu:', gpu)
    os.environ['CUDA_VISIBLE_DEVICES'] = gpu


def ensure_path(path, remove=True):
    basename = os.path.basename(path.rstrip('/'))
    if os.path.exists(path):
        if remove and (basename.startswith('_')
                       or input('{} exists, remove? ([y]/n): '.format(path)) != 'n'):
            shutil.rmtree(path)
            os.makedirs(path)
    else:
        os.makedirs(path)


def time_str(t):
    if t >= 3600:
        return '{:.1f}h'.format(t / 3600)
    if t >= 60:
        return '{:.1f}m'.format(t / 60)
    return '{:.1f}s'.format(t)


def compute_logits(feat, proto, metric='dot', temp=1.0):
    assert feat.dim() == proto.dim()

    if feat.dim() == 2:
        if metric == 'dot':
            logits = torch.mm(feat, proto.t())
        elif metric == 'cos':
            logits = torch.mm(F.normalize(feat, dim=-1),
                              F.normalize(proto, dim=-1).t())
        elif metric == 'sqr':
            logits = -(feat.unsqueeze(1) -
                       proto.unsqueeze(0)).pow(2).sum(dim=-1)

    elif feat.dim() == 3:
        if metric == 'dot':
            logits = torch.bmm(feat, proto.permute(0, 2, 1))
        elif metric == 'cos':
            logits = torch.bmm(F.normalize(feat, dim=-1),
                               F.normalize(proto, dim=-1).permute(0, 2, 1))
        elif metric == 'sqr':
            logits = -(feat.unsqueeze(2) -
                       proto.unsqueeze(1)).pow(2).sum(dim=-1)

    return logits * temp


def compute_acc(logits, label, reduction='mean'):
    ret = (torch.argmax(logits, dim=1) == label).float()
    if reduction == 'none':
        return ret.detach()
    elif reduction == 'mean':
        return ret.mean()


def compute_n_params(model, return_str=True):
    tot = 0
    for p in model.parameters():
        w = 1
        for x in p.shape:
            w *= x
        tot += w
    if return_str:
        if tot >= 1e6:
            return '{:.1f}M'.format(tot / 1e6)
        else:
            return '{:.1f}K'.format(tot / 1e3)
    else:
        return tot


def make_optimizer(params, name, max_steps, lr, weight_decay=None, milestones=None, scheduler='step', use_sam=False, sam_rho=0.005, eps=1e-8, **kwargs):
    if weight_decay is None:
        weight_decay = 0.
    if use_sam:
        optimizer = SAM(params, AdamW, rho=sam_rho, lr=lr, weight_decay=weight_decay, eps=1e-08)
    else:
        if name == 'sgd':
            optimizer = SGD(params, lr, momentum=0.9, weight_decay=weight_decay)
        elif name == 'adam':
            optimizer = Adam(params, lr, weight_decay=weight_decay)
        elif name == 'adamw':
            optimizer = AdamW(
                params, float(lr), betas=(0.9, 0.999), eps=float(eps),
                weight_decay=weight_decay
            )

    update_lr_every_epoch = True
    if scheduler == 'step':
        if milestones:
            lr_scheduler = MultiStepLR(optimizer, milestones)
        else:
            lr_scheduler = None
    elif scheduler == 'onecycle':
        lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
			optimizer,
			lr,
			max_steps + 100,
        	pct_start=0.05,
			cycle_momentum=False,
			anneal_strategy='linear',
			final_div_factor=10000
		)
        update_lr_every_epoch = False
    elif scheduler == 'warmup_cosine':
        import pl_bolts
        lr_scheduler = pl_bolts.optimizers.lr_scheduler.LinearWarmupCosineAnnealingLR(optimizer, kwargs['warmup_epochs'], kwargs['max_epochs'], warmup_start_lr=kwargs['warmup_start_lr'], eta_min=0.0, last_epoch=-1)
    return optimizer, lr_scheduler, update_lr_every_epoch


def visualize_dataset(dataset, name, writer, n_samples=1):
    def get_data(dataset, i):
        if dataset.use_moco:
            return dataset.convert_raw(dataset[i][0][0])
        else:
            return dataset.convert_raw(dataset[i][0])

    for task_id in np.random.choice(dataset.n_tasks, n_samples, replace=False):
        pos_indices = [task_id * dataset.bong_size * 2 + i
                       for i in range(dataset.bong_size)]
        data_per_task_pos = torch.stack([get_data(dataset, i)
                                         for i in pos_indices])
        neg_indices = [task_id * dataset.bong_size * 2 + i + dataset.bong_size
                       for i in range(dataset.bong_size)]
        data_per_task_neg = torch.stack([get_data(dataset, i)
                                         for i in neg_indices])

        if name is None:
            name = os.path.basename(dataset.tasks[task_id])
        else:
            name += '_' + os.path.basename(dataset.tasks[task_id])
        # stack 'L' to 'RGB' for visualization
        data_per_task_pos = torch.cat(
            [data_per_task_pos, data_per_task_pos, data_per_task_pos], dim=1)
        data_per_task_neg = torch.cat(
            [data_per_task_neg, data_per_task_neg, data_per_task_neg], dim=1)
        writer.add_images('visualize_' + name + '_task' + str(task_id) + '/pos',
                          data_per_task_pos)
        writer.add_images('visualize_' + name + '_task' + str(task_id) + '/neg',
                          data_per_task_neg)
    writer.flush()


def freeze_bn(model):
    for m in model.modules():
        if isinstance(m, nn.BatchNorm2d):
            m.eval()


class Logger(object):
    """
    Redirect stderr to stdout, optionally print stdout to a file,
    and optionally force flushing on both stdout and the file.
    """

    def __init__(self, file_name: str = None, file_mode: str = "w", should_flush: bool = True):
        self.file = None

        if file_name is not None:
            self.file = open(file_name, file_mode)

        self.should_flush = should_flush
        self.stdout = sys.stdout
        self.stderr = sys.stderr

        sys.stdout = self
        sys.stderr = self

    def __enter__(self) -> "Logger":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def write(self, text: str) -> None:
        """Write text to stdout (and a file) and optionally flush."""
        if len(text) == 0:  # workaround for a bug in VSCode debugger: sys.stdout.write(''); sys.stdout.flush() => crash
            return

        if self.file is not None:
            self.file.write(text)

        self.stdout.write(text)

        if self.should_flush:
            self.flush()

    def flush(self) -> None:
        """Flush written text to both stdout and a file, if open."""
        if self.file is not None:
            self.file.flush()

        self.stdout.flush()

    def close(self) -> None:
        """Flush, close possible files, and remove stdout/stderr mirroring."""
        self.flush()

        # if using multiple loggers, prevent closing in wrong order
        if sys.stdout is self:
            sys.stdout = self.stdout
        if sys.stderr is self:
            sys.stderr = self.stderr

        if self.file is not None:
            self.file.close()

def anytype2bool_dict(s):
    # check str
    if not isinstance(s, str):
        return s
    else:
        # try int
        try:
            ret = int(s)
        except:
            # try bool
            if s.lower() in ('true', 'false'):
                ret = s.lower() == 'true'
            # try float
            else:
                try:
                    ret = float(s)
                except:
                    ret = s
        return ret

def parse_string_to_dict(field_name, value):
    fields = field_name.split('.')
    for fd in fields[::-1]:
        res = {fd: anytype2bool_dict(value)}
        value = res
    return res

def merge_to_dicts(a, b):
    if isinstance(b, dict) and isinstance(a, dict):
        a_and_b = set(a.keys()) & set(b.keys())
        every_key = set(a.keys()) | set(b.keys())
        return {k: merge_to_dicts(a[k], b[k]) if k in a_and_b else
                   deepcopy(a[k] if k in a else b[k]) for k in every_key}
    return deepcopy(type(a)(b))

def override_cfg_from_list(cfg, opts):
    assert len(opts) % 2 == 0, 'Paired input must be provided to override config, opts: {}'.format(opts)
    for ix in range(0, len(opts), 2):
        opts_dict = parse_string_to_dict(opts[ix], opts[ix + 1])
        cfg = merge_to_dicts(cfg, opts_dict)
    return cfg

# ----------------------------------------------------------------------------

def find_free_port():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Binding to port 0 will cause the OS to find an available port for us
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    # NOTE: there is still a chance the port could be taken by other processes.
    return port

def get_world_size() -> int:
	if not dist.is_available():
		return 1
	if not dist.is_initialized():
		return 1
	return dist.get_world_size()


def get_rank() -> int:
	if not dist.is_available():
		return 0
	if not dist.is_initialized():
		return 0
	return dist.get_rank()


def get_local_rank() -> int:
	"""
	Returns:
		The rank of the current process within the local (per-machine) process group.
	"""
	if not dist.is_available():
		return 0
	if not dist.is_initialized():
		return 0
	assert _LOCAL_PROCESS_GROUP is not None
	return dist.get_rank(group=_LOCAL_PROCESS_GROUP)


def get_local_size() -> int:
	"""
	Returns:
		The size of the per-machine process group,
		i.e. the number of processes per machine.
	"""
	if not dist.is_available():
		return 1
	if not dist.is_initialized():
		return 1
	return dist.get_world_size(group=_LOCAL_PROCESS_GROUP)


def is_main_process() -> bool:
	return get_rank() == 0


def synchronize():
	"""
	Helper function to synchronize (barrier) among all processes when
	using distributed training
	"""
	if not dist.is_available():
		return
	if not dist.is_initialized():
		return
	world_size = dist.get_world_size()
	if world_size == 1:
		return
	dist.barrier()