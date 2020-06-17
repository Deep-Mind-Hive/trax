# coding=utf-8
# Copyright 2020 The Trax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Tests for trax.supervised.tf_inputs."""

import collections
import os

import gin
import numpy as np
from t5.data import preprocessors as t5_processors
import tensorflow as tf
import tensorflow_datasets as tfds
from trax.supervised import inputs  # pylint: disable=unused-import
from trax.supervised import tf_inputs


pkg_dir, _ = os.path.split(__file__)
_TESTDATA = os.path.join(pkg_dir, 'testdata')


def _test_dataset_ints(lengths):
  """Create a test dataset of int64 tensors of shape [length]."""
  def generator():
    """Sample generator of sequences of shape [length] of type int64."""
    for length in lengths:
      x = np.zeros([length], dtype=np.int64)
      yield (x, x)  # Inputs and targets are the same here.
  types = (tf.int64, tf.int64)
  shapes = (tf.TensorShape([None]), tf.TensorShape([None]))
  return tf.data.Dataset.from_generator(
      generator, output_types=types, output_shapes=shapes)


def _load_dataset(name, split='train'):
  return tfds.load(
      name=name, split=split, data_dir=_TESTDATA, shuffle_files=False)


def _c4_dataset(split='train'):
  return _load_dataset('c4', split=split)


def _spm_path():
  return os.path.join(_TESTDATA, 'sentencepiece.model')


def _t5_gin_config():
  # The following pages worth of gin configuration are required because a lot
  # of T5 functions have `gin.REQUIRED` in code, i.e. you cannot use these
  # functions at all without having configured gin.

  noise_density = 0.15
  max_input_length = 50

  # What preprocessors to apply - we select a random chunk of the document if
  # it exceeds a certain lengths (`select_random_chunk`), the concat multiple
  # documents together to reduce padding (`reduce_concat_tokens`), then split
  # up long examples (`split_tokens`) and finally the denoising objective
  # (`denoise`).
  gin.bind_parameter('unsupervised.preprocessors', [
      t5_processors.select_random_chunk,
      t5_processors.reduce_concat_tokens,
      t5_processors.split_tokens,
      t5_processors.denoise,
  ])

  # select_random_chunk
  gin.bind_parameter('select_random_chunk.feature_key', 'targets')
  gin.bind_parameter('select_random_chunk.max_length', max_input_length)

  # reduce_concat_tokens
  gin.bind_parameter('random_spans_helper.extra_tokens_per_span_inputs', 1)
  gin.bind_parameter('random_spans_helper.extra_tokens_per_span_targets', 1)
  gin.bind_parameter('random_spans_helper.inputs_length', max_input_length)
  gin.bind_parameter('random_spans_helper.mean_noise_span_length', 3.0)
  gin.bind_parameter('random_spans_helper.noise_density', noise_density)

  # split_tokens
  gin.bind_parameter('split_tokens.max_tokens_per_segment',
                     t5_processors.random_spans_tokens_length())

  # denoise
  gin.bind_parameter('denoise.inputs_fn',
                     t5_processors.noise_span_to_unique_sentinel)
  gin.bind_parameter('denoise.noise_density', noise_density)
  gin.bind_parameter('denoise.noise_mask_fn',
                     t5_processors.random_spans_noise_mask)
  gin.bind_parameter('denoise.targets_fn',
                     t5_processors.nonnoise_span_to_unique_sentinel)


class TFInputsTest(tf.test.TestCase):

  def setUp(self):
    super().setUp()
    gin.clear_config()

  def test_c4_bare_preprocess_fn(self):
    dataset = _c4_dataset()

    example = list(tfds.as_numpy(dataset.take(1)))[0]

    # Targets are NOT in the example.
    self.assertNotIn('targets', example)
    self.assertIn('text', example)
    text = example['text']

    # This should convert the dataset to an inputs/targets that are tokenized.
    dataset = tf_inputs.c4_bare_preprocess_fn(dataset, spm_path=_spm_path())

    example = list(tfds.as_numpy(dataset.take(1)))[0]

    # Earlier text is now stored in targets_plaintext
    self.assertIn('targets_plaintext', example)
    self.assertEqual(example['targets_plaintext'], text)

    # Targets are now tokenized.
    self.assertIn('targets', example)
    self.assertIsInstance(example['targets'], np.ndarray)
    self.assertEqual(example['targets'].dtype, np.int64)
    self.assertGreater(len(example['targets']), 0)

    # Inputs exist but is empty because t5 preprocessors' unsupervised wasn't
    # gin configured with any.
    self.assertIn('inputs', example)
    self.assertEqual(len(example['inputs']), 0)

  def test_c4_preprocess(self):
    def load_c4_dataset(split='train'):
      dataset = _c4_dataset(split=split)
      return dataset.map(lambda example: (example, example['text']))

    def examine_processed_dataset(proc_dataset):
      count = 0
      lengths = []
      for example in tfds.as_numpy(proc_dataset):
        count += 1
        ex = example[0]
        # Targets are in the example.
        self.assertIn('targets', ex)
        self.assertEqual(ex['targets'].dtype, np.int64)
        lengths.append(len(ex['targets']))
      return count, lengths

    unfiltered_count = 0
    for example in tfds.as_numpy(load_c4_dataset()):
      unfiltered_count += 1
      # Targets are NOT in the example.
      self.assertNotIn('targets', example[0])

    proc_dataset = tf_inputs.c4_preprocess(load_c4_dataset(), False, 2048)

    # `examine_processed_dataset` has some asserts in it.
    proc_count, char_lengths = examine_processed_dataset(proc_dataset)

    # Both the original and filtered datasets have examples.
    self.assertGreater(unfiltered_count, 0)
    self.assertGreater(proc_count, 0)

    # Because we filter out some entries on length.
    self.assertLess(proc_count, unfiltered_count)

    # Preprocess using the sentencepiece model in testdata.
    spc_proc_dataset = tf_inputs.c4_preprocess(
        load_c4_dataset(), False, 2048, tokenization='spc',
        spm_path=_spm_path())

    spc_proc_count, spc_lengths = examine_processed_dataset(spc_proc_dataset)

    # spc shortens the target sequence a lot, should be almost equal to
    # unfiltered
    self.assertLessEqual(proc_count, spc_proc_count)
    self.assertEqual(unfiltered_count, spc_proc_count)

    # Assert all spc_lengths are lesser than their char counterparts.
    for spc_len, char_len in zip(spc_lengths, char_lengths):
      self.assertLessEqual(spc_len, char_len)

  def test_c4(self):
    gin.bind_parameter('c4_preprocess.max_target_length', 2048)
    gin.bind_parameter('c4_preprocess.tokenization', 'spc')
    gin.bind_parameter('c4_preprocess.spm_path', _spm_path())

    # Just make sure this doesn't throw.
    _ = tf_inputs.data_streams(
        'c4', data_dir=_TESTDATA, input_name='targets', target_name='text',
        preprocess_fn=tf_inputs.c4_preprocess)

  def test_c4_bare_preprocess_fn_denoising_objective(self):
    _t5_gin_config()

    dataset = _c4_dataset()
    dataset = tf_inputs.c4_bare_preprocess_fn(dataset, spm_path=_spm_path())

    example = list(tfds.as_numpy(dataset.take(1)))[0]

    # Assertions now.

    self.assertIn('targets', example)
    targets = example['targets']
    self.assertIsInstance(targets, np.ndarray)
    self.assertEqual(targets.dtype, np.int64)
    self.assertGreater(len(targets), 0)

    self.assertIn('inputs', example)
    _inputs = example['inputs']  # pylint: disable=invalid-name
    self.assertIsInstance(_inputs, np.ndarray)
    self.assertEqual(_inputs.dtype, np.int64)
    self.assertGreater(len(_inputs), 0)

    # WHP inputs will have the bulk of the text.
    self.assertGreater(len(_inputs), len(targets))

    # WHP there will be two sentinel tokens in the inputs and targets.
    inputs_counter = collections.Counter(_inputs.tolist())
    targets_counter = collections.Counter(targets.tolist())
    self.assertEqual(1, inputs_counter[31999])
    self.assertEqual(1, inputs_counter[31998])
    self.assertEqual(1, targets_counter[31999])
    self.assertEqual(1, targets_counter[31998])

  def test_c4_pretrain(self):
    _t5_gin_config()

    gin.bind_parameter('c4_bare_preprocess_fn.spm_path', _spm_path())

    gin.bind_parameter('batcher.batch_size_per_device', 8)
    gin.bind_parameter('batcher.eval_batch_size', 8)
    gin.bind_parameter('batcher.max_eval_length', 50)
    gin.bind_parameter('batcher.buckets', ([51], [8, 1]))

    # Just make sure this doesn't throw.
    _ = tf_inputs.data_streams(
        'c4', data_dir=_TESTDATA, input_name='inputs', target_name='targets',
        bare_preprocess_fn=tf_inputs.c4_bare_preprocess_fn)

  def test_generic_text_dataset_preprocess_fn(self):
    dataset = _load_dataset('squad')

    example, = tfds.as_numpy(dataset.take(1))

    self.assertNotIn('inputs', example)
    self.assertNotIn('targets', example)

    proc_dataset = tf_inputs.generic_text_dataset_preprocess_fn(
        dataset, spm_path=_spm_path(),
        text_preprocess_fn=t5_processors.squad,
        copy_plaintext=True,
        debug_print_examples=True,
        debug_print_examples_rate=1.0)

    proc_example, = tfds.as_numpy(proc_dataset.take(1))

    self.assertIn('inputs', proc_example)
    self.assertIn('targets', proc_example)

    self.assertEqual(proc_example['inputs'].dtype, np.int64)
    self.assertEqual(proc_example['targets'].dtype, np.int64)

  def test_inputs_using_generic_text_dataset_preprocess_fn(self):

    gin.bind_parameter(
        'generic_text_dataset_preprocess_fn.spm_path', _spm_path())
    gin.bind_parameter(
        'generic_text_dataset_preprocess_fn.copy_plaintext', True)
    gin.bind_parameter(
        'generic_text_dataset_preprocess_fn.text_preprocess_fn',
        t5_processors.squad)

    # Just make sure this doesn't throw.
    def data_streams():
      return tf_inputs.data_streams(
          'squad', data_dir=_TESTDATA, input_name='inputs',
          target_name='targets',
          bare_preprocess_fn=tf_inputs.generic_text_dataset_preprocess_fn)

    squad_inputs = inputs.batcher(
        data_streams=data_streams,
        batch_size_per_device=2,
        eval_batch_size=2,
        max_eval_length=50,
    )

    n_devices = 3
    train_stream = squad_inputs.train_stream(n_devices)
    inps, tgts = next(train_stream)

    # We can only assert that the batch dim gets divided by n_devices.
    self.assertEqual(inps.shape[0] % n_devices, 0)
    self.assertEqual(tgts.shape[0] % n_devices, 0)

  def test_filter_dataset_on_len(self):
    def gen():
      for i in range(1, 11):
        yield {
            'inputs': np.ones((i,), dtype=np.int64),
            'targets': np.ones((2 * i,), dtype=np.int64),
        }

    ds = tf.data.Dataset.from_generator(
        gen,
        {
            'inputs': tf.int64,
            'targets': tf.int64,
        },
        {
            'inputs': tf.TensorShape([None]),
            'targets': tf.TensorShape([None]),
        },
    )

    ds1 = tf_inputs.filter_dataset_on_len(ds, {'inputs': 1, 'targets': 2})
    self.assertLen(list(ds1.as_numpy_iterator()), 1)

    ds2 = tf_inputs.filter_dataset_on_len(ds, {'inputs': 5, 'targets': 20})
    self.assertLen(list(ds2.as_numpy_iterator()), 5)

    ds3 = tf_inputs.filter_dataset_on_len(ds, {'inputs': 10, 'targets': 10})
    self.assertLen(list(ds3.as_numpy_iterator()), 5)

    ds4 = tf_inputs.filter_dataset_on_len(ds, {'inputs': 10, 'targets': 20})
    self.assertLen(list(ds4.as_numpy_iterator()), 10)

if __name__ == '__main__':
  tf.test.main()
