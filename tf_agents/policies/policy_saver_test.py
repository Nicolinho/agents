# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
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

"""Tests for PolicySaver."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

from absl.testing import parameterized
import tensorflow as tf

from tf_agents.agents.dqn import q_network
from tf_agents.environments import time_step as ts
from tf_agents.networks import q_rnn_network
from tf_agents.policies import policy_saver
from tf_agents.policies import q_policy
from tf_agents.specs import tensor_spec


class PolicySaverTest(tf.test.TestCase, parameterized.TestCase):

  def setUp(self):
    super(PolicySaverTest, self).setUp()
    self._time_step_spec = ts.TimeStep(
        step_type=tensor_spec.BoundedTensorSpec(
            dtype=tf.int32, shape=(), name='st',
            minimum=0, maximum=2),
        reward=tensor_spec.BoundedTensorSpec(
            dtype=tf.float32, shape=(), name='reward',
            minimum=0.0, maximum=5.0),
        discount=tensor_spec.BoundedTensorSpec(
            dtype=tf.float32, shape=(), name='discount',
            minimum=0.0, maximum=1.0),
        observation=tensor_spec.BoundedTensorSpec(
            dtype=tf.float32, shape=(4,), name='obs',
            minimum=-10.0, maximum=10.0))
    self._action_spec = tensor_spec.BoundedTensorSpec(
        dtype=tf.int32, shape=(), minimum=0, maximum=10, name='act_0')
    self._global_seed = 12345
    tf.compat.v1.set_random_seed(self._global_seed)

  @parameterized.named_parameters(('NotSeededNoState', False, False),
                                  ('NotSeededWithState', False, True),
                                  ('SeededNoState', True, False),
                                  ('SeededWithState', True, True))
  def testSaveAction(self, seeded, has_state):
    if not tf.executing_eagerly():
      self.skipTest('b/129079730: PolicySaver does not work in TF1.x yet')

    if has_state:
      network = q_rnn_network.QRnnNetwork(
          input_tensor_spec=self._time_step_spec.observation,
          action_spec=self._action_spec)
    else:
      network = q_network.QNetwork(
          input_tensor_spec=self._time_step_spec.observation,
          action_spec=self._action_spec)

    policy = q_policy.QPolicy(
        time_step_spec=self._time_step_spec,
        action_spec=self._action_spec,
        q_network=network)

    action_seed = 98723

    saver = policy_saver.PolicySaver(policy, batch_size=None, seed=action_seed)
    path = os.path.join(self.get_temp_dir(), 'save_model_action')
    saver.save(path)

    reloaded = tf.compat.v2.saved_model.load(path)

    self.assertIn('action', reloaded.signatures)
    reloaded_action = reloaded.signatures['action']
    self._compare_input_output_specs(
        reloaded_action,
        expected_input_specs=(self._time_step_spec, policy.policy_state_spec),
        expected_output_spec=policy.policy_step_spec,
        batch_input=True)

    batch_size = 3

    action_inputs = tensor_spec.sample_spec_nest(
        (self._time_step_spec, policy.policy_state_spec),
        outer_dims=(batch_size,), seed=4)

    function_action_input_dict = dict(
        (spec.name, value) for (spec, value) in
        zip(tf.nest.flatten((self._time_step_spec, policy.policy_state_spec)),
            tf.nest.flatten(action_inputs)))

    # NOTE(ebrevdo): The graph-level seeds for the policy and the reloaded model
    # are equal, which in addition to seeding the call to action() and
    # PolicySaver helps ensure equality of the output of action() in both cases.
    self.assertEqual(reloaded_action.graph.seed, self._global_seed)
    action_output = policy.action(*action_inputs, seed=action_seed)

    # The seed= argument for the SavedModel action call was given at creation of
    # the PolicySaver.

    # This is the flat-signature function.
    reloaded_action_output_dict = reloaded_action(
        **function_action_input_dict)

    def match_dtype_shape(x, y, msg=None):
      self.assertEqual(x.shape, y.shape, msg=msg)
      self.assertEqual(x.dtype, y.dtype, msg=msg)

    # This is the non-flat function.
    if has_state:
      reloaded_action_output = reloaded.action(*action_inputs)
    else:
      # Try both cases: one with an empty policy_state and one with no
      # policy_state.  Compare them.

      # NOTE(ebrevdo): The first call to .action() must be stored in
      # reloaded_action_output because this is the version being compared later
      # against the true action_output and the values will change after the
      # first call due to randomness.
      reloaded_action_output = reloaded.action(*action_inputs)
      reloaded_action_output_no_input_state = reloaded.action(action_inputs[0])
      # Even with a seed, multiple calls to action will get different values,
      # so here we just check the signature matches.
      tf.nest.map_structure(match_dtype_shape,
                            reloaded_action_output_no_input_state,
                            reloaded_action_output)

    action_output_dict = dict((
        (spec.name, value) for (spec, value) in
        zip(tf.nest.flatten(policy.policy_step_spec),
            tf.nest.flatten(action_output))))

    # Check output of the flattened signature call.
    action_output_dict = self.evaluate(action_output_dict)
    reloaded_action_output_dict = self.evaluate(reloaded_action_output_dict)
    self.assertAllEqual(
        action_output_dict.keys(), reloaded_action_output_dict.keys())

    for k in action_output_dict:
      if seeded:
        self.assertAllClose(
            action_output_dict[k],
            reloaded_action_output_dict[k],
            msg='\nMismatched dict key: %s.' % k)
      else:
        match_dtype_shape(action_output_dict[k],
                          reloaded_action_output_dict[k],
                          msg='\nMismatch dict key: %s.' % k)

    # Check output of the proper structured call.
    action_output = self.evaluate(action_output)
    reloaded_action_output = self.evaluate(reloaded_action_output)
    # With non-signature functions, we can check that passing a seed does the
    # right thing the second time.
    if seeded:
      tf.nest.map_structure(
          self.assertAllClose, action_output, reloaded_action_output)
    else:
      tf.nest.map_structure(
          match_dtype_shape, action_output, reloaded_action_output)

  def testSaveGetInitialState(self):
    if not tf.executing_eagerly():
      self.skipTest('b/129079730: PolicySaver does not work in TF1.x yet')

    network = q_rnn_network.QRnnNetwork(
        input_tensor_spec=self._time_step_spec.observation,
        action_spec=self._action_spec)

    policy = q_policy.QPolicy(
        time_step_spec=self._time_step_spec,
        action_spec=self._action_spec,
        q_network=network)

    saver_nobatch = policy_saver.PolicySaver(policy, batch_size=None)
    path = os.path.join(self.get_temp_dir(),
                        'save_model_initial_state_nobatch')
    saver_nobatch.save(path)
    reloaded_nobatch = tf.compat.v2.saved_model.load(path)
    self.assertIn('get_initial_state', reloaded_nobatch.signatures)
    reloaded_get_initial_state = (
        reloaded_nobatch.signatures['get_initial_state'])
    self._compare_input_output_specs(
        reloaded_get_initial_state,
        expected_input_specs=(
            tf.TensorSpec(dtype=tf.int32, shape=(), name='batch_size'),),
        expected_output_spec=policy.policy_state_spec,
        batch_input=False,
        batch_size=None)

    initial_state = policy.get_initial_state(batch_size=3)
    initial_state = self.evaluate(initial_state)

    reloaded_nobatch_initial_state = reloaded_nobatch.get_initial_state(
        batch_size=3)
    reloaded_nobatch_initial_state = self.evaluate(
        reloaded_nobatch_initial_state)
    tf.nest.map_structure(
        self.assertAllClose, initial_state, reloaded_nobatch_initial_state)

    saver_batch = policy_saver.PolicySaver(policy, batch_size=3)
    path = os.path.join(self.get_temp_dir(),
                        'save_model_initial_state_batch')
    saver_batch.save(path)
    reloaded_batch = tf.compat.v2.saved_model.load(path)
    self.assertIn('get_initial_state', reloaded_batch.signatures)
    reloaded_get_initial_state = reloaded_batch.signatures['get_initial_state']
    self._compare_input_output_specs(
        reloaded_get_initial_state,
        expected_input_specs=(),
        expected_output_spec=policy.policy_state_spec,
        batch_input=False,
        batch_size=3)

    reloaded_batch_initial_state = reloaded_batch.get_initial_state()
    reloaded_batch_initial_state = self.evaluate(reloaded_batch_initial_state)
    tf.nest.map_structure(
        self.assertAllClose, initial_state, reloaded_batch_initial_state)

  def _compare_input_output_specs(self,
                                  function,
                                  expected_input_specs,
                                  expected_output_spec,
                                  batch_input,
                                  batch_size=None):
    args, kwargs = function.structured_input_signature
    self.assertFalse(args)

    def expected_spec(spec, include_batch_dimension):
      if include_batch_dimension:
        return tf.TensorSpec(
            dtype=spec.dtype,
            shape=tf.TensorShape([batch_size]).concatenate(spec.shape),
            name=spec.name)
      else:
        return spec

    expected_input_spec_dict = dict(
        (spec.name, expected_spec(spec, include_batch_dimension=batch_input))
        for spec in tf.nest.flatten(expected_input_specs))
    expected_output_spec_dict = dict(
        (spec.name, expected_spec(spec, include_batch_dimension=True))
        for spec in tf.nest.flatten(expected_output_spec))

    self.assertEqual(kwargs, expected_input_spec_dict)
    self.assertEqual(function.structured_outputs, expected_output_spec_dict)


if __name__ == '__main__':
  tf.test.main()
