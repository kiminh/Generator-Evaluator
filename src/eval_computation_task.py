#!/usr/bin/env python
# coding=utf8
# File: train.py
from __future__ import print_function
import sys
import copy
import os
from os.path import exists
import numpy as np
from sklearn.metrics import roc_auc_score
import logging

PARL_DIR = os.environ['PARL_DIR']
sys.path.append(PARL_DIR)

import parl.layers as layers
from parl.layers.layer_wrappers import LayerFunc
from paddle import fluid
from paddle.fluid.executor import _fetch_var
from paddle.fluid.framework import Variable

from utils import TracebackWrapper, save_pickle
from fluid_utils import executor_run_with_fetch_dict, parallel_executor_run_with_fetch_dict

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

class EvalComputationTask(object):
    """
    For evaluation tasks
    """
    def __init__(self, algorithm, model_dir='', mode='single', mode_args={}, scope=None):
        """
        Args:
            algorithm: Algorithm object in PARL
        """
        self.alg = algorithm
        self.model_dir = model_dir
        self.mode = mode
        self.mode_args = mode_args
        self.ckp_step = -1
        self.use_cuda = True if self.alg.gpu_id >= 0 else False

        self.scope = scope
        with fluid.scope_guard(self.scope):
            self._define_program()
            self._define_executor(mode)

    def _define_program(self):
        """
        Use fluid.unique_name to make sure train 
        and test are using the same params 
        if the model is not base on PARL.
        """
        self.train_program = fluid.Program()
        self.startup_program = fluid.Program()
        self.test_program = fluid.Program()
        self.inference_program = fluid.Program()        # only consider single mode

        with fluid.program_guard(self.train_program, self.startup_program):
            with fluid.unique_name.guard():
                self.train_outputs = self.alg.train()

        with fluid.program_guard(self.test_program, fluid.Program()):   # the test startup program is not used.
            with fluid.unique_name.guard():
                self.test_outputs = self.alg.test()

        with fluid.program_guard(self.inference_program, fluid.Program()):
            with fluid.unique_name.guard():
                self.inference_outputs = self.alg.inference()

    def _define_executor(self, mode):
        """
        define executors, run startup, and load saved models
        """
        if mode == 'single':
            place = fluid.CUDAPlace(0) if self.use_cuda else fluid.CPUPlace()
            self.base_exe = fluid.Executor(place)
            self.base_exe.run(self.startup_program)
            self.ckp_step = self.load_model(self.model_dir)
            self.train_exe = self.base_exe
            self.test_exe = self.base_exe

        elif mode == 'parallel':
            place = fluid.CUDAPlace(0) if self.use_cuda else fluid.CPUPlace()
            self.base_exe = fluid.Executor(place)
            self.base_exe.run(self.startup_program)
            self.ckp_step = self.load_model(self.model_dir)
            self._define_parallel_executor(self.train_program, self.test_program)

    def _define_parallel_executor(self, train_program, test_program):
        strategy = fluid.ExecutionStrategy()
        if self.use_cuda:
            strategy.num_threads = 1            # otherwise it will crash in GPU mode. 
        # strategy.allow_op_delay = False
        build_strategy = fluid.BuildStrategy()
        loss = self.train_outputs['fetch_dict']['loss']
        self.train_exe = fluid.ParallelExecutor(use_cuda=self.use_cuda, 
                                                loss_name=loss.name,
                                                main_program=train_program,
                                                exec_strategy=strategy,
                                                build_strategy=build_strategy,
                                                scope=self.scope)
        self.test_exe = fluid.ParallelExecutor(use_cuda=self.use_cuda, 
                                                share_vars_from=self.train_exe,
                                                main_program=test_program,
                                                exec_strategy=strategy,
                                                build_strategy=build_strategy,
                                                scope=self.scope)

    ###################
    ### main functions
    ###################

    def train(self, list_feed_dict):
        """train"""
        if self.mode == 'single':
            assert len(list_feed_dict) == 1
            return executor_run_with_fetch_dict(self.train_exe, 
                                                program=self.train_program,
                                                fetch_dict=self.train_outputs['fetch_dict'],
                                                feed=list_feed_dict[0],
                                                return_numpy=False,
                                                scope=self.scope)

        elif self.mode == 'parallel':
            return parallel_executor_run_with_fetch_dict(self.train_exe,
                                                         fetch_dict=self.train_outputs['fetch_dict'],
                                                         feed=list_feed_dict,
                                                         return_numpy=False)

    def test(self, list_feed_dict):
        """test"""
        if self.mode == 'single':
            assert len(list_feed_dict) == 1
            return executor_run_with_fetch_dict(self.test_exe, 
                                                program=self.test_program,
                                                fetch_dict=self.test_outputs['fetch_dict'],
                                                feed=list_feed_dict[0],
                                                return_numpy=False,
                                                scope=self.scope)

        elif self.mode == 'parallel':
            return parallel_executor_run_with_fetch_dict(self.test_exe,
                                                         fetch_dict=self.test_outputs['fetch_dict'],
                                                         feed=list_feed_dict,
                                                         return_numpy=False)

    def inference(self, feed_dict):
        """inference"""
        return executor_run_with_fetch_dict(self.base_exe, 
                                            program=self.inference_program,
                                            fetch_dict=self.inference_outputs['fetch_dict'],
                                            feed=feed_dict,
                                            return_numpy=False,
                                            scope=self.scope)

    ##############
    ### utils
    ##############

    def print_var_shapes(self):
        for param in self.train_program.global_block().all_parameters():
            array = np.array(self.scope.find_var(param.name).get_tensor())
            if len(array.shape) == 2:
                print (param.name, array.shape, array[0, :4])
            elif len(array.shape) == 1:
                print (param.name, array.shape, array[:4])

    def save_model(self, path, checkpoint_step):
        if not exists(path):
            os.makedirs(path)
        with fluid.scope_guard(self.scope):
            fluid.io.save_persistables(executor=self.base_exe,
                                     dirname=path,
                                     main_program=self.train_program,
                                     filename='model-%d.ckp' % checkpoint_step)
        logging.info ('==> Model saved to %s' % path)

    def load_model(self, path, ckp_step=None):
        if ckp_step == None:
            ckp_step = self.get_lastest_checkpoint(path)
        if ckp_step >= 0:
            with fluid.scope_guard(self.scope):
                fluid.io.load_persistables(executor=self.base_exe,
                                         dirname=path,
                                         main_program=self.train_program,
                                         filename='model-%d.ckp' % ckp_step)
        logging.info ('==> Model loaded from %s (step = %d)' % (path, ckp_step))
        return ckp_step
    
    def get_lastest_checkpoint(self, path):
        last_ckp_step = -1
        if not exists(path):
            return last_ckp_step

        files = os.listdir(path)
        prefix = 'model-'
        suffix = '.ckp'
        for f in files:
            if not (f.startswith(prefix) and f.endswith(suffix)):
                continue
            ckp_step = f[len(prefix):-len(suffix)]
            if not ckp_step.isdigit():
                continue
            ckp_step = int(ckp_step)
            last_ckp_step = max(last_ckp_step, ckp_step)
        return last_ckp_step







