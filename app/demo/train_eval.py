"""
train:
    train uni-rnn to predict:
        - 'click': the ctr
        - 'click_credit': the ctr and the credit
test:
    test the auc and the mse
generate_list:
    generate list by uni-rnn
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np
import sys
import math
import copy
import time
import datetime
import os
from os.path import basename, join, exists, dirname
import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s') # filename='hot_rl.log', 

#########
# envs
#########

import tensorflow as tf

import paddle
from paddle import fluid

from config import Config
from utils import BatchData, add_scalar_summary, sequence_unconcat

import _init_paths

from src.eval_net import BiRNN, Transformer
from src.eval_algorithm import EvalAlgorithm
from src.eval_computation_task import EvalComputationTask

from src.utils import (read_json, print_args, tik, tok, threaded_generator, print_once,
                        AUCMetrics, AssertEqual, SequenceRMSEMetrics, SequenceCorrelationMetrics)
from src.fluid_utils import (fluid_create_lod_tensor as create_tensor, 
                            concat_list_array, seq_len_2_lod, get_num_devices)
from data.npz_dataset import NpzDataset

#########
# utils
#########

def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exp', help="Exp id, used for logs or savings")
    parser.add_argument('--use_cuda', default = 1, type = int, help = "")
    parser.add_argument('--train_mode', 
                        default = 'single', 
                        choices = ['single', 'parallel'],
                        type = str, 
                        help = "single: use the first gpu, parallel: use all gpus")
    parser.add_argument('--task', 
                        default = 'train', 
                        choices = ['train', 'test', 'eval', 'debug'],
                        type = str, 
                        help = "")
    
    # model settings
    parser.add_argument('--model', type=str, choices=['BiRNN', 'Trans'], help='')
    return parser


class EvalFeedConvertor(object):
    @staticmethod
    def train_test(batch_data):
        place = fluid.CPUPlace()
        feed_dict = {}
        for name in batch_data.conf.user_slot_names + batch_data.conf.item_slot_names:
            ft = batch_data.tensor_dict[name]
            feed_dict[name] = create_tensor(ft.values, lod=ft.lod, place=place)

        click_id = batch_data.tensor_dict['click_id']
        feed_dict['click_id'] = create_tensor(click_id.values, lod=click_id.lod, place=place)
        return feed_dict

    @staticmethod
    def inference(batch_data):
        place = fluid.CPUPlace()
        feed_dict = {}
        for name in batch_data.conf.user_slot_names + batch_data.conf.item_slot_names:
            ft = batch_data.tensor_dict[name]
            feed_dict[name] = create_tensor(ft.values, lod=ft.lod, place=place)            
        return feed_dict


############
# main
############

def main(args):
    print_args(args, 'args')
    conf = Config(args.exp)

    ### build model
    npz_config = read_json(conf.npz_config_path)
    scope = fluid.Scope()
    with fluid.scope_guard(scope):
        with fluid.unique_name.guard():
            if args.model == 'BiRNN':
                model = BiRNN(conf, npz_config)
            elif args.model == 'Trans':
                model = Transformer(conf, npz_config, num_blocks=2, num_head=4)

            algorithm = EvalAlgorithm(model, optimizer=conf.optimizer, lr=conf.lr, gpu_id=(0 if args.use_cuda == 1 else -1))
            td_ct = EvalComputationTask(algorithm, model_dir=conf.model_dir, mode=args.train_mode, scope=scope)

    ### other tasks
    if args.task == 'test':
        test(td_ct, args, conf, None, td_ct.ckp_step)
        exit()
    elif args.task == 'debug':
        debug(td_ct, args, conf, None, td_ct.ckp_step)
        exit()
    elif args.task == 'eval':
        return td_ct

    ### start training
    summary_writer = tf.summary.FileWriter(conf.summary_dir)
    for epoch_id in range(td_ct.ckp_step + 1, conf.max_train_steps):
        train(td_ct, args, conf, summary_writer, epoch_id)
        td_ct.save_model(conf.model_dir, epoch_id)
        test(td_ct, args, conf, summary_writer, epoch_id)


def train(td_ct, args, conf, summary_writer, epoch_id):
    """train for conf.train_interval steps"""
    dataset = NpzDataset(conf.train_npz_list, conf.npz_config_path, conf.requested_npz_names, if_random_shuffle=True)
    data_gen = dataset.get_data_generator(conf.batch_size)

    list_epoch_loss = []
    list_loss = []
    batch_id = 0
    for tensor_dict in data_gen:
        batch_data = BatchData(conf, tensor_dict)
        fetch_dict = td_ct.train(EvalFeedConvertor.train_test(batch_data))
        list_loss.append(np.array(fetch_dict['loss']))
        list_epoch_loss.append(np.mean(np.array(fetch_dict['loss'])))
        if batch_id % conf.prt_interval == 0:
            logging.info('batch_id:%d loss:%f' % (batch_id, np.mean(list_loss)))
            list_loss = []
        batch_id += 1

    add_scalar_summary(summary_writer, epoch_id, 'train/loss', np.mean(list_epoch_loss))


def test(td_ct, args, conf, summary_writer, epoch_id):
    """eval auc on the full test dataset"""
    dataset = NpzDataset(conf.test_npz_list, conf.npz_config_path, conf.requested_npz_names, if_random_shuffle=False)
    data_gen = dataset.get_data_generator(conf.batch_size)

    auc_metric = AUCMetrics()
    seq_rmse_metric = SequenceRMSEMetrics()
    seq_correlation_metric = SequenceCorrelationMetrics()
    batch_id = 0
    for tensor_dict in data_gen:
        batch_data = BatchData(conf, tensor_dict)
        fetch_dict = td_ct.test(EvalFeedConvertor.train_test(batch_data))
        click_id = np.array(fetch_dict['click_id']).flatten()
        click_prob = np.array(fetch_dict['click_prob'])[:, 1]
        click_id_unconcat = sequence_unconcat(click_id, batch_data.seq_lens())
        click_prob_unconcat = sequence_unconcat(click_prob, batch_data.seq_lens())
        auc_metric.add(labels=click_id, y_scores=click_prob)
        for sub_click_id, sub_click_prob in zip(click_id_unconcat, click_prob_unconcat):
            seq_rmse_metric.add(labels=sub_click_id, preds=sub_click_prob)
            seq_correlation_metric.add(labels=sub_click_id, preds=sub_click_prob)

        batch_id += 1

    add_scalar_summary(summary_writer, epoch_id, 'test/auc', auc_metric.overall_auc())
    add_scalar_summary(summary_writer, epoch_id, 'test/seq_rmse', seq_rmse_metric.overall_rmse())
    add_scalar_summary(summary_writer, epoch_id, 'test/seq_correlation', seq_correlation_metric.overall_correlation())


def debug(td_ct, args, conf, summary_writer, epoch_id):
    pass


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    main(args) 


