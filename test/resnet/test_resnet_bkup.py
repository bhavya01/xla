from torch_xla.experimental import pjrt
import random
from torch.utils.data import Dataset
import args_parse
from lars import create_optimizer_lars
from lars_utils import *
from itertools import islice
import resnet_model
from PIL import Image

SUPPORTED_MODELS = [
    'alexnet', 'densenet121', 'densenet161', 'densenet169', 'densenet201',
    'inception_v3', 'resnet101', 'resnet152', 'resnet18', 'resnet34',
    'resnet50', 'squeezenet1_0', 'squeezenet1_1', 'vgg11', 'vgg11_bn', 'vgg13',
    'vgg13_bn', 'vgg16', 'vgg16_bn', 'vgg19', 'vgg19_bn'
]

MODEL_OPTS = {
    '--model': {
        'choices': SUPPORTED_MODELS,
        'default': 'resnet50',
    },
    '--test_set_batch_size': {
        'type': int,
    },
    '--lr_scheduler_type': {
        'type': str,
    },
    '--lr_scheduler_divide_every_n_epochs': {
        'type': int,
    },
    '--lr_scheduler_divisor': {
        'type': int,
    },
    '--test_only_at_end': {
        'action': 'store_true',
    },
    '--ddp': {
        'action': 'store_true',
    },
    # Use pjrt:// init_method instead of env:// for `torch.distributed`.
    # Required for DDP on TPU v2/v3 when using PJRT.
    '--pjrt_distributed': {
        'action': 'store_true',
    },
    '--profile': {
        'action': 'store_true',
    },
    '--persistent_workers': {
        'action': 'store_true',
    },
    '--prefetch_factor': {
        'type': int,
    },
    '--loader_prefetch_size': {
        'type': int,
    },
    '--device_prefetch_size': {
        'type': int,
    },
    '--host_to_device_transfer_threads': {
        'type': int,
    },
    '--num_train_steps': {
        'type': int,
    },
    '--lars': {
        'action': 'store_true',
    },
    '--base_lr': {
        'type': float,
    },
    '--eeta': {
        'type': float,
    },
    '--end_lr': {
        'type': float,
    },
    '--epsilon': {
        'type': float,
    },
    '--weight_decay': {
        'type': float,
    },
    '--num_steps_per_epoch': {
        'type': int,
    },
    '--warmup_epochs': {
        'type': int,
    },
    '--label_smoothing': {
        'type': float,
    },
    '--enable_space_to_depth': {
        'action': 'store_true',
    },
    '--num_classes': {
        'type': int,
    },
    '--use_optimized_kwargs': {
        'type': str,
    },
    '--amp': {
        'action': 'store_true',
    },
    # Using zero gradients optimization for AMP
    '--use_zero_grad': {
        'action': 'store_true',
    },
    # Using sync_free optimizer for AMP
    '--use_syncfree_optim': {
        'action': 'store_true',
    }
}

FLAGS = args_parse.parse_common_options(
    datadir='/tmp/imagenet',
    batch_size=None,
    num_epochs=None,
    momentum=None,
    lr=None,
    target_accuracy=None,
    profiler_port=9012,
    opts=MODEL_OPTS.items(),
)

import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
from torchvision import datasets
import torch_xla
import torch_xla.debug.metrics as met
import torch_xla.distributed.parallel_loader as pl
import torch_xla.debug.profiler as xp
import torch_xla.utils.utils as xu
import torch_xla.core.xla_model as xm
import torch_xla.distributed.xla_multiprocessing as xmp
import torch_xla.test.test_utils as test_utils
from torch_xla.amp import autocast, GradScaler
try:
  from torch_xla.amp import syncfree
except ImportError:
  assert False, "Missing package syncfree; the package is available in torch-xla>=1.11"
import torch.distributed as dist
import torch_xla.distributed.xla_backend

DEFAULT_KWARGS = dict(
    batch_size=128,
    test_set_batch_size=128,
    num_epochs=44,
    momentum=0.9,
    lr=0.1,
    target_accuracy=0.0,
    persistent_workers=True,
    prefetch_factor=32,
    loader_prefetch_size=128,
    device_prefetch_size=1,
    num_workers=16,
    host_to_device_transfer_threads=4,
    num_train_steps=6864,
    num_steps_per_epoch=156,
    base_lr=17,
    eeta=1e-3,
    epsilon=0.0,
    weight_decay=2e-4,
    warmup_epochs=5,
    end_lr=0.0,
    label_smoothing=0.1,
    num_classes = 1000, 

)

#  Best config to achieve peak performance based on TPU version
#    1. It is recommended to use this config in conjuntion with XLA_USE_BF16=1 Flag.
#    2. Hyperparameters can be tuned to further improve the accuracy.
#  usage: python3 /usr/share/pytorch/xla/test/test_train_mp_imagenet.py --model=resnet50 \
#         --fake_data --num_epochs=10 --log_steps=300 \
#         --profile   --use_optimized_kwargs=tpuv4  --drop_last
OPTIMIZED_KWARGS = {
    'tpuv4':
        dict(
            batch_size=128,
            test_set_batch_size=128,
            num_epochs=18,
            momentum=0.9,
            lr=0.1,
            target_accuracy=0.0,
            persistent_workers=True,
            prefetch_factor=32,
            loader_prefetch_size=128,
            device_prefetch_size=1,
            num_workers=16,
            host_to_device_transfer_threads=4,
        )
}

MODEL_SPECIFIC_DEFAULTS = {
    # Override some of the args in DEFAULT_KWARGS/OPTIMIZED_KWARGS, or add them to the dict
    # if they don't exist.
    'resnet50':
        dict(
            OPTIMIZED_KWARGS.get(FLAGS.use_optimized_kwargs, DEFAULT_KWARGS),
            **{
                'lr': 0.5,
                'lr_scheduler_divide_every_n_epochs': 20,
                'lr_scheduler_divisor': 5,
                'lr_scheduler_type': 'WarmupAndExponentialDecayScheduler',
            })
}

# Set any args that were not explicitly given by the user.
default_value_dict = MODEL_SPECIFIC_DEFAULTS.get(FLAGS.model, DEFAULT_KWARGS)
for arg, value in default_value_dict.items():
  if getattr(FLAGS, arg) is None:
    setattr(FLAGS, arg, value)


class InfiniteImageNetDataset(Dataset):
    def __init__(self, root_dir, transform=None):
        self.root_dir = root_dir
        self.transform = transform

        self.image_paths = []
        self.labels = []
        self.cache = {}

        for label, dname in enumerate(sorted(os.listdir(self.root_dir))):
            for fname in os.listdir(os.path.join(self.root_dir, dname)):
                self.image_paths.append(os.path.join(self.root_dir, dname, fname))
                self.labels.append(label)

        self.image_paths = np.array(self.image_paths)
        self.labels = np.array(self.labels)
        seed = 42
        self.rng = np.random.default_rng(seed)

    def __len__(self):
        return 1281024*45  # Infinite length

    def __getitem__(self, idx):
        # Modulo operation to make the dataset repeat indefinitely
        #if idx != 0 and idx % self.num_images == 0:
        #    perm = np.random.permutation(self.num_images)
        #    self.image_paths = self.image_paths[perm]
        #    self.labels = self.labels[perm]
        idx = idx % len(self.image_paths)
        if idx in self.cache:
          image = self.cache[idx]
        else:
        #random_idx = self.rng.integers(len(self.image_paths))
          image = Image.open(self.image_paths[idx]).convert('RGB')
          self.cache[idx] = image
        if self.transform:
            image = self.transform(image)

        label = self.labels[idx]

        return image, label

from torch.utils.data import DataLoader
class InfiniteDataLoader(DataLoader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize an iterator over the dataset.
        self.dataset_iterator = super().__iter__()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            batch = next(self.dataset_iterator)
        except StopIteration:
            # Dataset exhausted, use a new fresh iterator.
            random.shuffle(self.dataset.imgs)
            self.dataset_iterator = super().__iter__()
            batch = next(self.dataset_iterator)
        return batch

def _train_update(device, step, loss, tracker, epoch, writer):
  test_utils.print_training_update(
      device,
      step,
      loss.item(),
      tracker.rate(),
      tracker.global_rate(),
      epoch,
      summary_writer=writer)
  #xm.master_print(f'loss: {loss.item()}')

def train_imagenet():
  if FLAGS.pjrt_distributed:
    import torch_xla.experimental.pjrt_backend
    dist.init_process_group('xla', init_method='pjrt://')
  elif FLAGS.ddp:
    dist.init_process_group(
        'xla', world_size=xm.xrt_world_size(), rank=xm.get_ordinal())

  print('==> Preparing data..')
  img_dim = 224
  if FLAGS.fake_data:
    train_dataset_len = 1200000  # Roughly the size of Imagenet dataset.
    train_loader = xu.SampleGenerator(
        data=(torch.rand(FLAGS.batch_size, 3, img_dim, img_dim),
              torch.randint(1000,(FLAGS.batch_size,), dtype=torch.int64)),
        sample_count=train_dataset_len)
    test_loader = xu.SampleGenerator(
        data=(torch.rand(FLAGS.test_set_batch_size, 3, img_dim, img_dim),
              torch.randint(1000,(FLAGS.test_set_batch_size,), dtype=torch.int64)),
        sample_count=50000 // FLAGS.batch_size // xm.xrt_world_size())
  else:
    normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    train_dataset = InfiniteImageNetDataset(
        os.path.join(FLAGS.datadir, 'train'),
        transforms.Compose([
            transforms.RandomResizedCrop(img_dim),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.ConvertImageDtype(torch.bfloat16),
            normalize,
        ]))
    train_dataset_len = 1281024
    resize_dim = max(img_dim, 256)
    test_dataset = torchvision.datasets.ImageFolder(
        os.path.join(FLAGS.datadir, 'val'),
        # Matches Torchvision's eval transforms except Torchvision uses size
        # 256 resize for all models both here and in the train loader. Their
        # version crashes during training on 299x299 images, e.g. inception.
        transforms.Compose([
            transforms.Resize(resize_dim),
            transforms.CenterCrop(img_dim),
            transforms.ToTensor(),
            transforms.ConvertImageDtype(torch.bfloat16),
            normalize,
        ]))

    train_sampler, test_sampler = None, None
    if xm.xrt_world_size() > 1:
      train_sampler = torch.utils.data.distributed.DistributedSampler(
          train_dataset,
          num_replicas=xm.xrt_world_size(),
          rank=xm.get_ordinal(),
          shuffle=True)
      test_sampler = torch.utils.data.distributed.DistributedSampler(
          test_dataset,
          num_replicas=xm.xrt_world_size(),
          rank=xm.get_ordinal(),
          shuffle=False)
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=FLAGS.batch_size,
        sampler=train_sampler,
        drop_last=False,
        shuffle=False if train_sampler else True,
        num_workers=FLAGS.num_workers,
        persistent_workers=FLAGS.persistent_workers,
        prefetch_factor=FLAGS.prefetch_factor)

    device = xm.xla_device()
    test_loader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=FLAGS.test_set_batch_size,
        sampler=test_sampler,
        drop_last=FLAGS.drop_last,
        shuffle=False,
        num_workers=8,
        persistent_workers=FLAGS.persistent_workers,
        prefetch_factor=16)
    train_device_loader = pl.MpDeviceLoader(
      train_loader,
      #islice(train_loader, 12000),
      device,
      loader_prefetch_size=FLAGS.loader_prefetch_size,
      device_prefetch_size=FLAGS.device_prefetch_size,
      host_to_device_transfer_threads=FLAGS.host_to_device_transfer_threads)
    test_device_loader = pl.MpDeviceLoader(
      test_loader,
      device,
      loader_prefetch_size=8,
      device_prefetch_size=4,
      host_to_device_transfer_threads=1)
  torch.manual_seed(42)

  device_hw = xm.xla_device_hw(device)
  model =  resnet_model.Resnet50(FLAGS.num_classes).to(device)

  # Initialization is nondeterministic with multiple threads in PjRt.
  # Synchronize model parameters across replicas manually.
  if pjrt.using_pjrt():
    pjrt.broadcast_master_param(model)

  if FLAGS.ddp:
    model = DDP(model, gradient_as_bucket_view=True, broadcast_buffers=False)

  writer = None
  if xm.is_master_ordinal():
    writer = test_utils.get_summary_writer(FLAGS.logdir)

  optimizer = create_optimizer_lars(model = model,
                                        lr = FLAGS.base_lr,
                                        eeta = FLAGS.eeta,
                                        epsilon=FLAGS.epsilon,
                                        momentum=FLAGS.momentum,
                                        weight_decay=FLAGS.weight_decay,
                                        bn_bias_separately=True)
  num_training_steps_per_epoch = train_dataset_len // (
      FLAGS.batch_size * xm.xrt_world_size())
  lr_scheduler = PolynomialWarmup(optimizer, decay_steps=FLAGS.num_epochs * FLAGS.num_steps_per_epoch,
                                    warmup_steps=FLAGS.warmup_epochs * FLAGS.num_steps_per_epoch,
                                    end_lr=0.0, power=2.0, last_epoch=-1)

  loss_fn = LabelSmoothLoss(FLAGS.label_smoothing)
  if FLAGS.amp:
    if device_hw == 'TPU':
      scaler = None
    elif device_hw == 'GPU':
      scaler = GradScaler(use_zero_grad=FLAGS.use_zero_grad)

  if FLAGS.profile:
    server = xp.start_server(FLAGS.profiler_port)

  def train_loop_fn(loader, epoch):
    tracker = xm.RateTracker()
    model.train()
    for step, (data, target) in enumerate(loader):
      with xp.StepTrace('train_imagenet'):
        with xp.Trace('build_graph'):
          optimizer.zero_grad()
          if FLAGS.amp:
            with autocast(xm.xla_device()):
              output = model(data)
              loss = loss_fn(output, target)
            if scaler:
              scaler.scale(loss).backward()
              gradients = xm._fetch_gradients(optimizer)
              xm.all_reduce('sum', gradients, scale=1.0 / xm.xrt_world_size())
              scaler.step(optimizer)
              scaler.update()
            else:
              loss.backward()
              xm.optimizer_step(optimizer)
          else:
            output = model(data)
            loss = loss_fn(output, target)
            loss.backward()
            xm.optimizer_step(optimizer)
          tracker.add(FLAGS.batch_size)
          if lr_scheduler:
            lr_scheduler.step()
      if (step+1) % FLAGS.log_steps == 0:
          
        #xm.mark_step()
        xm.add_step_closure(
            _train_update, args=(device, step, loss, tracker, epoch, writer))
        '''
        accuracy = test_loop_fn(test_device_loader, epoch)
        xm.master_print('Epoch {} test end {}, Accuracy={:.2f}'.format(
          epoch, test_utils.now(), accuracy))
        test_utils.write_to_summary(
         writer,
         epoch,
         dict_to_write={'Accuracy/test': accuracy},
         write_xla_metrics=True)
        model.train()
        '''

  def test_loop_fn(loader, epoch):
    total_samples, correct = 0, 0
    model.eval()
    total_samples = 0
    for step, (data, target) in enumerate(loader):
      with xp.Trace('eval_imagenet'):
        output = model(data)
        pred = output.max(1, keepdim=True)[1]
        correct += pred.eq(target.view_as(pred)).sum()
        total_samples += data.size()[0]
        if (step+1) % FLAGS.log_steps == 0:
          xm.add_step_closure(
              test_utils.print_test_update, args=(device, None, epoch, step))
    accuracy = 0.0
    try:
      accuracy = 100.0 * correct.item() / total_samples
    except:
      pass
    if accuracy > 0.0:
      accuracy = xm.mesh_reduce('test_accuracy', accuracy, np.mean)
    return accuracy

  

  accuracy, max_accuracy = 0.0, 0.0
  for epoch in range(1, FLAGS.num_epochs + 1):
    xm.master_print('Epoch {} train begin {}'.format(epoch, test_utils.now()))
    train_loop_fn(train_device_loader, epoch)
    xm.master_print('Epoch {} train end {}'.format(epoch, test_utils.now()))
    '''
    if epoch:
      accuracy = test_loop_fn(test_device_loader, epoch)
      xm.master_print('Epoch {} test end {}, Accuracy={:.2f}'.format(
          epoch, test_utils.now(), accuracy))
      max_accuracy = max(accuracy, max_accuracy)
      test_utils.write_to_summary(
          writer,
          epoch,
          dict_to_write={'Accuracy/test': accuracy},
          write_xla_metrics=True)
    '''
    if FLAGS.metrics_debug:
      xm.master_print(met.metrics_report())
  test_utils.close_summary_writer(writer)
  xm.master_print('Max Accuracy: {:.2f}%'.format(max_accuracy))
  return max_accuracy


def _mp_fn(index, flags):
  global FLAGS
  FLAGS = flags
  torch.set_default_tensor_type('torch.FloatTensor')
  accuracy = train_imagenet()
  if accuracy < FLAGS.target_accuracy:
    print('Accuracy {} is below target {}'.format(accuracy,
                                                  FLAGS.target_accuracy))
    sys.exit(21)


if __name__ == '__main__':
  xmp.spawn(_mp_fn, args=(FLAGS,), nprocs=FLAGS.num_cores)