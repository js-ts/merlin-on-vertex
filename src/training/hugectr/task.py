# Copyright 2021 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#            http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and

"""DeepFM Network trainer."""

import argparse
import hypertune
import json
import logging
import os
import time
import shutil

import hugectr

from hugectr.inference import InferenceParams, CreateInferenceSession
from trainer.model import create_model
from trainer import utils

MODEL_PREFIX = 'deepfm'
SNAPSHOT_DIR = 'snapshots'
GRAPH_DIR = 'graph'
MODEL_PARAMETERS_DIR = 'parameters'
HYPERTUNE_METRIC_NAME = 'AUC'

LOCAL_MODEL_DIR = '/tmp/saved_model'
LOCAL_CHECKPOINT_DIR = '/tmp/checkpoints'


def set_job_dirs():
    """Sets job directories based on env variables set by Vertex AI."""
    
    model_dir = os.getenv('AIP_MODEL_DIR', LOCAL_MODEL_DIR)
    if model_dir[0:5] == 'gs://':
        model_dir = model_dir.replace('gs://', '/gcs/')
    checkpoint_dir = os.getenv('AIP_CHECKPOINT_DIR', LOCAL_CHECKPOINT_DIR)
    if checkpoint_dir[0:5] == 'gs://':
        checkpoint_dir = checkpoint_dir.replace('gs://', '/gcs/')
    
    return model_dir, checkpoint_dir


def save_model(model, model_dir):
    """Saves model graph and model parameters."""
    
    graph_path = os.path.join(model_dir, GRAPH_DIR)
    parameters_path = os.path.join(model_dir, MODEL_PARAMETERS_DIR)
                                   
    if os.path.isdir(graph_path):
        shutil.rmtree(graph_path)
    os.makedirs(graph_path)
    
    if os.path.isdir(parameters_path):
        shutil.rmtree(parameters_path)
    os.makedirs(parameters_path)
                     
    graph_path = os.path.join(graph_path, f'{MODEL_PREFIX}.json')
    logging.info('Saving model graph to: {}'.format(graph_path))  
    
    model.graph_to_json(graph_config_file=graph_path)
   
    parameters_path = os.path.join(parameters_path, MODEL_PREFIX)
    logging.info('Saving model parameters to: {}'.format(parameters_path)) 
    model.save_params_to_files(prefix=parameters_path)
    

def evaluate_model(
    model_dir, 
    eval_data_source,
    num_batches,
    slot_size_array,
    max_batchsize=2048, 
    hit_rate_threshold=0.6,
    device_id=0,
    use_gpu_embedding_cache=True,
    cache_size_percentage=0.6,
    i64_input_key=True):
    """Evaluates a model on a validation dataset."""
    
    dense_model_file = os.path.join(model_dir, MODEL_PARAMETERS_DIR, f'{MODEL_PREFIX}_dense_0.model')
    sparse_model_files = [os.path.join(model_dir, MODEL_PARAMETERS_DIR, f'{MODEL_PREFIX}0_sparse_0.model')]
    
    inference_params = InferenceParams(model_name=MODEL_PREFIX,
                                       max_batchsize=max_batchsize,
                                       hit_rate_threshold=hit_rate_threshold,
                                       dense_model_file=dense_model_file,
                                       sparse_model_files=sparse_model_files,
                                       device_id=device_id,
                                       use_gpu_embedding_cache=use_gpu_embedding_cache,
                                       cache_size_percentage=cache_size_percentage,
                                       i64_input_key=i64_input_key)
    
    model_config_path = os.path.join(model_dir, GRAPH_DIR, f'{MODEL_PREFIX}.json')
    inference_session = CreateInferenceSession(model_config_path=model_config_path, 
                                               inference_params=inference_params)
    
    eval_results = inference_session.evaluate(num_batches=num_batches,
                                              source=eval_data_source,
                                              data_reader_type=hugectr.DataReaderType_t.Parquet,
                                              check_type=hugectr.Check_t.Non,
                                              slot_size_array=slot_size_array)
    return eval_results
                                              
                               
        
    
def main(args):
    """Runs a training loop."""

    repeat_dataset = False if args.num_epochs > 0 else True
    model_dir, snapshot_dir = set_job_dirs()
    num_gpus = len(args.gpus)
    batch_size = num_gpus * args.per_gpu_batch_size if args.num_gpus  else args.per_gpu_batch_size 
    
    model = create_model(train_data=[args.train_data],
                         valid_data=args.valid_data,
                         max_eval_batches=args.max_eval_batches,
                         dropout_rate=args.dropout_rate,
                         num_dense_features=args.num_dense_features,
                         num_sparse_features=args.num_sparse_features,
                         num_workers=args.num_workers,
                         slot_size_array=args.slot_size_array,
                         batchsize=batch_size,
                         lr=args.lr,
                         gpus=args.gpus,
                         repeat_dataset=repeat_dataset)

    model.summary()
    
    logging.info('Starting model training')
    model.fit(num_epochs=args.num_epochs,
              max_iter=args.max_iter,
              display=args.display_interval, 
              eval_interval=args.eval_interval, 
              snapshot=args.snapshot_interval, 
              snapshot_prefix=os.path.join(snapshot_dir, MODEL_PREFIX))
    
    logging.info('Saving model')
    save_model(model, model_dir)
    
    logging.info('Starting model evaluation using {} batches ...'.format(args.eval_batches))
    metric_value = evaluate_model(model_dir=model_dir, 
                         eval_data_source=args.valid_data,
                         num_batches=args.eval_batches,         
                         max_batchsize=args.batchsize,
                         slot_size_array=args.slot_size_array)
    logging.info('{} on the evaluation dataset: {}'.format(HYPERTUNE_METRIC_NAME, metric_value))
    
    # Report AUC to Vertex hypertuner
    logging.info('Reporting {} metric at {} to Vertex hypertuner'.format(HYPERTUNE_METRIC_NAME, metric_value))
    hpt = hypertune.HyperTune()
    hpt.report_hyperparameter_tuning_metric(
        hyperparameter_metric_tag=HYPERTUNE_METRIC_NAME,
        metric_value=metric_value,
        global_step=args.max_iter if repeat_dataset else args.num_epochs)
    
    

def parse_args():
    """Parses command line arguments."""

    parser = argparse.ArgumentParser()
    parser.add_argument('-t',
                        '--train_data',
                        type=str,
                        required=True,
                        help='Path to training data _file_list.txt')
    parser.add_argument('-v',
                        '--valid_data',
                        type=str,
                        required=True,
                        help='Path to validation data _file_list.txt')
    parser.add_argument('--schema',
                        type=str,
                        required=True,
                        help='Path to the schema.pbtxt file')
    parser.add_argument('--dropout_rate',
                        type=float,
                        required=False,
                        default=0.5,
                        help='Dropout rate')
    parser.add_argument('--num_dense_features',
                        type=int,
                        required=False,
                        default=13,
                        help='Number of dense features')
    parser.add_argument('--num_sparse_features',
                        type=int,
                        required=False,
                        default=26,
                        help='Number of sparse features')
    parser.add_argument('--lr',
                        type=float,
                        required=False,
                        default=0.001,
                        help='Learning rate')
    parser.add_argument('-i',
                        '--max_iter',
                        type=int,
                        required=False,
                        default=0,
                        help='Number of training iterations')
    parser.add_argument('--max_eval_batches',
                        type=int,
                        required=False,
                        default=100,
                        help='Max eval batches for evaluations during model.fit()')
    parser.add_argument('--eval_batches',
                        type=int,
                        required=False,
                        default=100,
                        help='Number of evaluation batches for the final evaluation')
    parser.add_argument('--num_epochs',
                        type=int,
                        required=False,
                        default=1,
                        help='Number of training epochs')
    parser.add_argument('-b',
                        '--per_gpu_batch_size',
                        type=int,
                        required=False,
                        default=2048,
                        help='Per GPU Batch size')
    parser.add_argument('-s',
                        '--snapshot_interval',
                        type=int,
                        required=False,
                        default=10000,
                        help='Saves a model snapshot after given number of iterations')
    parser.add_argument('--gpus',
                        type=str,
                        required=False,
                        default="[[0]]",
                        help='GPU devices to use for Preprocessing')
    parser.add_argument('-r',
                        '--eval_interval',
                        type=int,
                        required=False,
                        default=1000,
                        help='Run evaluation after given number of iterations')
    parser.add_argument('--display_interval',
                        type=int,
                        required=False,
                        default=100,
                        help='Display progress after given number of iterations')
    parser.add_argument('--workspace_size_per_gpu',
                        type=int,
                        required=False,
                        default=61,
                        help='Workspace size per gpu in MB')
    parser.add_argument('--num_workers',
                        type=int,
                        required=False,
                        default=12,
                        help='Number of workers')


    args = parser.parse_args()

    return args  

if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO, datefmt='%d-%m-%y %H:%M:%S')
    
    args = parse_args()
    
    logging.info('Extracting cardinalities from schema...')
    cardinalities = utils.retrieve_cardinalities(args.schema)
    logging.info('Cardinalities are extracted.')
    
    slot_size_array = [int(cardinality) for cardinality in cardinalities.values()]

   
    args.gpus = json.loads(args.gpus)
    args.slot_size_array = json.loads(slot_size_array)

    logging.info(f"Args: {args}")
    start_time = time.time()
    logging.info("Starting training")

    main(args)

    end_time = time.time()
    elapsed_time = end_time - start_time
    logging.info("Training completed. Elapsed time: {}".format(elapsed_time))