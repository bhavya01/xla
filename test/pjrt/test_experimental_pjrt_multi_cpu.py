import itertools
import os

from absl.testing import absltest, parameterized

import torch
import torch_xla.core.xla_model as xm
import torch_xla.core.xla_env_vars as xenv
from torch_xla.experimental import pjrt


class TestExperimentalPjrtMultiCpu(parameterized.TestCase):

  def setUp(self):
    pjrt.set_device_type('CPU')

    os.environ.update({
        xenv.PJRT_CPU_ASYNC_CLIENT: 'true',
        xenv.CPU_NUM_DEVICES: '4',
    })

  def test_default_cpu_device(self):
    os.environ.pop(xenv.CPU_NUM_DEVICES, None)
    os.environ.pop(xenv.PJRT_CPU_ASYNC_CLIENT, None)

    expected = {0: {0: torch.device('xla:0'),}}
    devices_per_process = pjrt.run_multiprocess(xm.xla_device)
    self.assertDictEqual(devices_per_process, expected)

  def test_multi_cpu_devices(self):
    expected = {
        0: {
            0: torch.device('xla:0'),
            1: torch.device('xla:1'),
            2: torch.device('xla:2'),
            3: torch.device('xla:3')
        }
    }

    devices_per_process = pjrt.run_multiprocess(xm.xla_device)
    self.assertDictEqual(devices_per_process, expected)


  @parameterized.named_parameters(('xla_model', xm.get_ordinal),
                                  ('pjrt', pjrt.global_ordinal))
  def test_global_ordinal(self, ordinal_func):
    results = pjrt.run_multiprocess(ordinal_func)
    values = list(
        itertools.chain.from_iterable(row.values() for row in results.values()))
    self.assertListEqual(sorted(values), [0, 1, 2, 3])


  @parameterized.named_parameters(('xla_model', xm.get_local_ordinal),
                                  ('pjrt', pjrt.local_ordinal))
  def test_local_ordinal(self, ordinal_func):
    # TODO(wcromar): add multiprocess tests
    results = pjrt.run_multiprocess(ordinal_func)
    values = list(
        itertools.chain.from_iterable(row.values() for row in results.values()))
    self.assertListEqual(sorted(values), [0, 1, 2, 3])


  @staticmethod
  def _multi_cpu_backwards():
    results = {}

    class _CustomBackwards(torch.autograd.Function):
      @staticmethod
      def forward(ctx, x):
        rank = xm.get_ordinal()
        ctx.forward_rank = rank
        return x

      @staticmethod
      def backward(ctx, grad_output):
        results['forward_ordinal'] = ctx.forward_rank
        results['backward_ordinal'] = xm.get_ordinal()
        results['device'] = str(xm.xla_device())
        return grad_output

    x = torch.ones(1, requires_grad=True, device=xm.xla_device())
    y = _CustomBackwards.apply(x)
    y.backward()
    xm.mark_step()

    return results

  def test_multi_cpu_backwards(self):
    os.environ.update({
        xenv.PJRT_CPU_ASYNC_CLIENT: 'true',
        xenv.CPU_NUM_DEVICES: '4',
    })

    expected = {
      0: {i: {'forward_ordinal': i, 'backward_ordinal': i, 'device': f'xla:{i}'} for i in range(4)}
    }
    results = pjrt.run_multiprocess(self._multi_cpu_backwards)

    self.assertDictEqual(results, expected)


if __name__ == '__main__':
  absltest.main()
