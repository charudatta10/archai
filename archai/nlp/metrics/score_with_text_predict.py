# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Score models using Text Predict.
"""

from __future__ import annotations

import argparse
import os

from archai.nlp.metrics.text_predict.predictor import run_score

from archai.common import utils


def _check_amulet_paths(args: argparse.Namespace) -> argparse.Namespace:
    # Makes sure that AMLT-based runnings works
    amlt_data_path = os.environ.get('AMLT_DATA_DIR', '')
    amlt_output_path = os.environ.get('AMLT_OUTPUT_DIR', '')

    if args.data_from_amlt:
        args.default_path = os.path.join(amlt_data_path, args.default_path)
    if args.model_from_amlt:
        args.model_path = os.path.join(amlt_output_path, args.model_path)
    if args.output_to_amlt:
        args.output_path = os.path.join(amlt_output_path, args.default_path)

    del args.data_from_amlt
    del args.model_from_amlt
    del args.output_to_amlt

    return args


def parse_args():
    parser = argparse.ArgumentParser(description='Score models with Text Predict.')

    paths = parser.add_argument_group('Scoring paths')
    paths.add_argument('--default_path',
                        type=str,
                        default='~/logdir',
                        help='Path to the default folder used to save outputs.')

    paths.add_argument('--model_path',
                        type=str,
                        default=None,
                        help='Path to the model to be loaded.')

    paths.add_argument('--vocab_path',
                        type=str,
                        default=None,
                        help='Path to the vocabulary to be loaded.')

    paths.add_argument('--input_file_path',
                        type=str,
                        default=None,
                        help='Path to the input file to be scored.')

    score = parser.add_argument_group('Scoring types')
    score.add_argument('--input_file_type',
                       type=str,
                       default='smartcompose',
                       choices=['smartcompose', 'text'],
                       help='Type of file to be scored.')

    score.add_argument('--model_type',
                       type=str,
                       default='mem_transformer',
                       choices=['hf_gpt2', 'hf_gpt2_flex', 'hf_transfo_xl', 'mem_transformer'],
                       help='Type of model to be searched.')

    score.add_argument('--with_onnx',
                       action='store_true',
                       help='Uses ONNX-based models instead of PyTorch.')

    hyperparameters = parser.add_argument_group('Scoring hyperparameters')
    hyperparameters.add_argument('--min_score',
                                 type=float,
                                 default=1.0,
                                 help='Minimum score used within the model.')

    hyperparameters.add_argument('--max_score',
                                 type=float,
                                 default=5.0,
                                 help='Maximum score used within the model.')

    hyperparameters.add_argument('--score_step',
                                 type=float,
                                 default=0.1,
                                 help='Step of the score used within the model.')

    hyperparameters.add_argument('--expected_match_rate',
                                 type=float,
                                 default=0.5,
                                 help='Expected match rate to score the model.')

    hyperparameters.add_argument('--current_paragraph_only',
                                 action='store_true',
                                 help='Uses only current paragraph to score the model.')

    hyperparameters.add_argument('--max_body_len',
                                 type=int,
                                 default=10000,
                                 help='Maximum length of the input sequences.')

    hyperparameters.add_argument('--min_pred_len',
                                 type=int,
                                 default=6,
                                 help='Minimum length of the predictions.')

    amlt = parser.add_argument_group('AMLT-based triggers')
    amlt.add_argument('--data_from_amlt',
                        action='store_true',
                        help='Whether incoming data is from AMLT.')

    amlt.add_argument('--model_from_amlt',
                        action='store_true',
                        help='Whether incoming model is from AMLT.')

    amlt.add_argument('--output_to_amlt',
                        action='store_true',
                        help='Whether output should go to AMLT.')
                    
    args, _ = parser.parse_known_args()
    args = _check_amulet_paths(args)
    
    return vars(args)


if __name__ == '__main__':
    # Gathers the command line arguments
    args = parse_args()

    # Defines remaining paths
    args['output_path'] = utils.full_path(os.path.join(args['default_path'], 'score'), create=True)
    
    # Runs the Text Predict scoring
    run_score(**args)
