from __future__ import print_function

import os
import sys
import argparse
import importlib
import numpy as np
import tensorflow as tf
import cPickle as pickle
from threading import Thread
from shapely.geometry import Polygon
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(ROOT_DIR, 'models'))
sys.path.append(os.path.join(ROOT_DIR, 'dataset'))
sys.path.append(os.path.join(ROOT_DIR, 'utils'))
sys.path.append(os.path.join(ROOT_DIR, 'kitti'))
from frustum_model_util import NUM_HEADING_BIN, NUM_SIZE_CLUSTER
from frustum_model_util import NUM_SEG_CLASSES, NUM_OBJ_CLASSES, g_type2onehotclass, type_whitelist
from frustum_dataset import FrustumDataset, Sample
from frustum_pointnets_v2 import FrustumPointNet
from rpn_dataset import Dataset
import provider
from kitti_object import *
import kitti_util as utils
import test_frustum

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=0, help='GPU to use [default: GPU 0]')
parser.add_argument('--num_point', type=int, default=16384, help='Point Number [default: 1024]')
parser.add_argument('--rpn_model', default='log_rpn/model.ckpt', help='rpn model checkpoint file path [default: log/model.ckpt]')
parser.add_argument('--img_seg_model', default='./frozen_inference_graph.pb', help='image segmentation model path')
parser.add_argument('--rcnn_model', default='log_rcnn/model.ckpt', help='rcnn model checkpoint file path [default: log/model.ckpt]')
parser.add_argument('--output', default='test_results', help='output file/folder name [default: test_results]')
parser.add_argument('--kitti_path', default='/data/ssd/public/jlliu/Kitti/object', help='Kitti root path')
parser.add_argument('--split', default='test', help='Data split to use [default: test]')
parser.add_argument('--dump_result', action='store_true', help='If true, also dump results to .pickle file')
FLAGS = parser.parse_args()

BATCH_SIZE = 1
BATCH_SIZE_RCNN = 8
GPU_INDEX = FLAGS.gpu
NUM_POINT = FLAGS.num_point
NUM_POINT_RCNN = 512

RPN_DATASET = Dataset(NUM_POINT, FLAGS.kitti_path, FLAGS.split, is_training=False)
RCNN_DATASET = FrustumDataset(NUM_POINT_RCNN, FLAGS.kitti_path, BATCH_SIZE_RCNN, FLAGS.split,
             data_dir='./rcnn_data_'+FLAGS.split,
             augmentX=1, random_shift=False, rotate_to_center=True, random_flip=False, use_gt_prop=False)

def get_session_and_models():
    ''' Define model graph, load model parameters,
    create session and return session handle and tensors
    '''
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    config.log_device_placement = False
    saver = tf.train.Saver()

    # image segmentaion
    with tf.Graph().as_default():
        with tf.device('/gpu:'+str(GPU_INDEX)):
            img_seg_net = ImgSegNet(BATCH_SIZE, NUM_POINT)
            img_seg_net.load_graph(FLAGS.img_seg_model)
        sess1 = tf.Session(config=config)

    # point RPN
    with tf.Graph().as_default():
        with tf.device('/gpu:'+str(GPU_INDEX)):
            rpn_model = RPN(BATCH_SIZE, NUM_POINT, num_channel=4, is_training=is_training)
            pls = rpn_model.placeholders
            box_center, box_angle, box_size = rpn_model.box_encoder.tf_decode(rpn_model.end_points)
            box_center = box_center + rpn_model.end_points['fg_points_xyz']
            rpn_model.end_points['box_center'] = box_center
            rpn_model.end_points['box_angle'] = box_angle
            rpn_model.end_points['box_size'] = box_size
        sess2 = tf.Session(config=config)
        saver.restore(sess1, FLAGS.rpn_model)

    with tf.Graph().as_default():
        with tf.device('/gpu:'+str(GPU_INDEX)):
            rcnn_model = FrustumPointNet(BATCH_SIZE_RCNN, NUM_POINT_RCNN)
        sess3 = tf.Session(config=config)
        saver.restore(sess3, FLAGS.rcnn_model)

    return sess1, img_seg_net, sess2, rpn_model, sess3, rcnn_model

def test(result_dir=None):
    rpn_dataset = Dataset(NUM_POINT, FLAGS.kitti_path, FLAGS.split, is_training=False)
    produce_thread = Thread(target=rpn_dataset.load, args=(False,))
    produce_thread.start()

    sess1, img_seg_net, sess2, rpn_model, sess3, rcnn_model = get_session_and_models()

    seg_pls = img_seg_net.placeholders
    rpn_pls = rpn_model.placeholders
    seg_softmax = img_seg_net.get_seg_softmax()
    full_seg = img_seg_net.get_semantic_seg()

    cls_list = []
    center_list = []
    heading_cls_list = []
    heading_res_list = []
    size_cls_list = []
    size_res_list = []
    rot_angle_list = []
    score_list = []
    prob_list = []
    frame_id_list = []
    proposal_score_list = []
    while(True):
        batch_data, is_last_batch = rpn_dataset.get_next_batch(BATCH_SIZE, need_id=True)
        start = datetime.now()
        img_seg_logits, full_img_seg = sess1.run([seg_softmax, full_seg], feed_dict={
            seg_pls['pointclouds']: batch_data['pointcloud'],
            seg_pls['img_inputs']: batch_data['images'],
            seg_pls['calib']: batch_data['calib'],
            seg_pls['seg_labels']: batch_data['seg_label']
        })
        # convert to binary segmentation
        img_seg_binary = np.zeros((BATCH_SIZE, NUM_POINT, 2))
        img_seg_binary[...,0] = img_seg_logits[...,0]
        img_seg_binary[...,1] = np.sum(img_seg_logits[...,1:], axis=-1)
        img_seg_binary *= np.array([0, 1]) # weights

        centers_val, angles_val, sizes_val, scores_val \
        = sess2.run(
            [rpn_model.end_points['box_center'],
            rpn_model.end_points['box_angle'],
            rpn_model.end_points['box_size'],
            rpn_model.end_points['proposal_scores']],
            feed_dict={
                rpn_pls['pointclouds']: batch_data['pointcloud'],
                rpn_pls['img_inputs']: batch_data['images'],
                rpn_pls['calib']: batch_data['calib'],
                rpn_pls['seg_labels']: batch_data['seg_label'],
                rpn_pls['center_bin_x_labels']: batch_data['center_x_cls'],
                rpn_pls['center_bin_z_labels']: batch_data['center_z_cls'],
                rpn_pls['center_x_residuals_labels']: batch_data['center_x_res'],
                rpn_pls['center_y_residuals_labels']: batch_data['center_y_res'],
                rpn_pls['center_z_residuals_labels']: batch_data['center_z_res'],
                rpn_pls['heading_bin_labels']: batch_data['angle_cls'],
                rpn_pls['heading_residuals_labels']: batch_data['angle_res'],
                rpn_pls['size_class_labels']: batch_data['size_cls'],
                rpn_pls['size_residuals_labels']: batch_data['size_res'],
                rpn_pls['gt_box_of_point']: batch_data['gt_box_of_point'],
                rpn_pls['img_seg_softmax']: img_seg_binary,
                rpn_pls['is_training_pl']: is_training,
            })

        # prepared data for rcnn
        rpn_out = {
            'frame_id': batch_data['ids'][0],
            'segmentation': preds_val[0],
            'centers': centers_val[0],
            'angles': angles_val[0],
            'sizes': sizes_val[0],
            'proposal_boxes': corners_val[0],
            'nms_indices': ind_val[0],
            'scores': scores_val[0],
            'pc_choices': batch_data['pc_choice'][0]
        }
        RCNN_DATASET.load_frame_data(batch_data['ids'][0], rpn_out, full_img_seg)

        # 2-stage
        while(True):
            batch_data_rcnn, is_last_batch_rcnn = RCNN_DATASET.get_next_batch()

        	batch_cls, batch_center_pred, \
            batch_hclass_pred, batch_hres_pred, \
            batch_sclass_pred, batch_sres_pred, batch_scores, batch_prob = \
            test_frustum.inference(sess3, rcnn_model,
                batch_data_rcnn['pointcloud'], batch_data_rcnn['img_seg_map'],
                batch_data_rcnn['prop_box'], batch_data_rcnn['calib'], batch_data_rcnn['cls_label'])
            # gather output of all frames
            for i in range(BATCH_SIZE_RCNN):
                cls_list.append(batch_cls[i,...])
                center_list.append(batch_center_pred[i,:])
                heading_cls_list.append(batch_hclass_pred[i])
                heading_res_list.append(batch_hres_pred[i])
                size_cls_list.append(batch_sclass_pred[i])
                size_res_list.append(batch_sres_pred[i,:])
                rot_angle_list.append(batch_rot_angle[i])
                score_list.append(batch_scores[i])
                prob_list.append(batch_prob[i])
                proposal_score_list.append(batch_proposal_score[i])
            frame_id_list += map(lambda fid: int(fid), batch_frame_ids)

            if is_last_batch_rcnn:
                break

        if is_last_batch:
            break

    type_list = map(lambda i: type_whitelist[i], cls_list)
    detection_objects = test_frustum.to_detection_objects(frame_id_list, type_list,
        center_list, heading_cls_list, heading_res_list,
        size_cls_list, size_res_list, rot_angle_list, score_list, prob_list,
        proposal_score_list)
    detection_objects = test_frustum.nms_on_bev(detection_objects, 0.01)
    # Write detection results for KITTI evaluation
    test_frustum.write_detection_results(result_dir, detection_objects)
    output_dir = os.path.join(result_dir, 'data')
    print('write detection results to ' + output_dir)
    # Make sure for each frame (no matter if we have measurment for that frame),
    # there is a TXT file
    to_fill_filename_list = [frame_id+'.txt' \
            for frame_id in TEST_DATASET.frame_ids]
    test_frustum.fill_files(output_dir, to_fill_filename_list)

    rpn_dataset.stop_loading()
    produce_thread.join()

if __name__=='__main__':
    test(FLAGS.output)
