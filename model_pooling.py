from tf_ops.graph_conv_layer import *
from tf_ops.graph_pooling_layer import *
import tensorflow.contrib.framework as framework
from functools import partial
from model import graph_max_pool_stage,graph_unpool_stage,classifier_v3,classifier_v5,graph_avg_pool_stage
from tensorflow.python.client import timeline
import numpy as np
from draw_util import output_points


def preprocess(xyzs,feats,labels):
    xyzs, pxyzs, dxyzs, feats, labels, vlens, vbegs, vcens=\
        points_pooling(xyzs,feats,labels,voxel_size=0.2,block_size=3.0)
    return xyzs, pxyzs, dxyzs, feats, labels, vlens, vbegs, vcens


def graph_conv_pool_block_edge_new(xyzs, feats, stage_idx, layer_idx, ofn, ncens, nidxs, nlens, nbegs, reuse):
    feats = tf.contrib.layers.fully_connected(feats, num_outputs=ofn, scope='{}_{}_fc'.format(stage_idx, layer_idx),
                                              activation_fn=tf.nn.relu, reuse=reuse)
    feats = graph_conv_edge(xyzs, feats, ofn, [ofn/2, ofn/2], ofn, nidxs, nlens, nbegs, ncens,
                            '{}_{}_gc'.format(stage_idx,layer_idx), reuse=reuse)
    return feats


def graph_conv_pool_block_edge_xyz_new(sxyzs, stage_idx, gxyz_dim, ncens, nidxs, nlens, nbegs, reuse):
    xyz_gc=graph_conv_edge_xyz_v2(sxyzs, gxyz_dim, [gxyz_dim/2, gxyz_dim/2], gxyz_dim, nidxs, nlens, nbegs, ncens,
                                  '{}_xyz_gc'.format(stage_idx),reuse=reuse)
    return xyz_gc


def graph_conv_pool_stage_edge_new(stage_idx, xyzs, dxyz, feats, feats_dim, gxyz_dim, gc_dims, gfc_dims, final_dim,
                                   radius, voxel_size, reuse):
    ops=[]
    with tf.name_scope('stage_{}'.format(stage_idx)):
        nidxs,nlens,nbegs,ncens=search_neighborhood(xyzs,radius)

        sxyzs = neighbor_ops.neighbor_scatter(xyzs, nidxs, nlens, nbegs, use_diff=True)  # [en,ifn]
        sxyzs /= radius   # rescale

        xyz_gc=graph_conv_pool_block_edge_xyz_new(sxyzs,stage_idx,gxyz_dim,ncens,nidxs,nlens,nbegs,reuse)
        ops.append(xyz_gc)
        cfeats = tf.concat([xyz_gc, feats], axis=1)

        cdim = feats_dim + gxyz_dim
        conv_fn = partial(graph_conv_pool_block_edge_new, ncens=ncens, nidxs=nidxs, nlens=nlens, nbegs=nbegs, reuse=reuse)

        layer_idx = 1
        for gd in gc_dims:
            conv_feats = conv_fn(sxyzs, cfeats, stage_idx, layer_idx, gd)
            cfeats = tf.concat([cfeats, conv_feats], axis=1)
            ops.append(conv_feats)
            layer_idx += 1
            cdim += gd

        with framework.arg_scope([tf.contrib.layers.fully_connected], activation_fn=tf.nn.relu, reuse=reuse):
            with tf.name_scope('fc_global{}'.format(stage_idx)):
                dxyz= dxyz / voxel_size

                fc = tf.concat([cfeats, dxyz], axis=1)
                for i, gfd in enumerate(gfc_dims):
                    fc = tf.contrib.layers.fully_connected(fc, num_outputs=gfd,
                                                           scope='{}_{}_gfc'.format(stage_idx, i))
                fc_final = tf.contrib.layers.fully_connected(fc, num_outputs=final_dim, activation_fn=None,
                                                             scope='{}_final_gfc'.format(stage_idx))

    return fc_final, cfeats, ops  # cfeats: [pn,fc_dims+gxyz_dim+feats_dim]


def graph_conv_pool_edge_new(xyzs, dxyzs, pxyzs, feats, vlens, vbegs, vcens, voxel_len, block_size, reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_new(0, xyzs, dxyzs, feats, tf.shape(feats)[1], radius=0.1, reuse=reuse,
                                                                gxyz_dim=8, gc_dims=[8,16], gfc_dims=[16,32,64], final_dim=64,
                                                                voxel_size=voxel_len)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens, vbegs)

            with tf.name_scope('conv_stage1'):
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_new(1, pxyzs, pxyzs, fc0_pool, 64, radius=0.5, reuse=reuse, voxel_size=block_size,
                                                                gxyz_dim=8, gc_dims=[32,32,64,64,128], gfc_dims=[128,256,384], final_dim=384)
                fc1_pool = tf.reduce_max(fc1, axis=0)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = tf.tile(tf.expand_dims(fc1_pool, axis=0), [tf.shape(fc1)[0], 1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens, vbegs, vcens)
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf = tf.concat([fc0, lf0], axis=1)

            ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            ops0+=ops1

    return upf0, lf, ops0


def graph_conv_pool_edge_new_v2(xyzs, dxyzs, pxyzs, feats, vlens, vbegs, vcens, voxel_size, block_size, reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                # 8 64 64*2
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_new(0, xyzs, dxyzs, feats, tf.shape(feats)[1], radius=0.1,
                                                                reuse=reuse, voxel_size=voxel_size,
                                                                gxyz_dim=16, gc_dims=[16,16,16,16,16],
                                                                gfc_dims=[64,64,64], final_dim=64)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens, vbegs)

            with tf.name_scope('conv_stage1'):
                # 16 288 512*2
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_new(1, pxyzs, pxyzs, fc0_pool, 64, radius=0.5,
                                                                reuse=reuse, voxel_size=block_size,
                                                                gxyz_dim=16, gc_dims=[32,32,32,64,64,64],
                                                                gfc_dims=[256,256,256], final_dim=512)
                fc1_pool = tf.reduce_max(fc1, axis=0)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = tf.tile(tf.expand_dims(fc1_pool, axis=0), [tf.shape(fc1)[0], 1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens, vbegs, vcens)
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf = tf.concat([fc0, lf0], axis=1)

            ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            ops0+=ops1
    # 1528 + 132
    return upf0, lf, ops0


def graph_conv_semantic_pool_stage(stage_idx, dxyz, feats, gfc_dims, final_dim, reuse):
    with tf.name_scope('stage_{}'.format(stage_idx)):
        with framework.arg_scope([tf.contrib.layers.fully_connected], activation_fn=tf.nn.relu, reuse=reuse):
            with tf.name_scope('fc_global{}'.format(stage_idx)):
                fc = tf.concat([feats, dxyz], axis=1)
                for i, gfd in enumerate(gfc_dims):
                    fc = tf.contrib.layers.fully_connected(fc, num_outputs=gfd,scope='{}_gfc{}'.format(stage_idx, i))
                fc_final = tf.contrib.layers.fully_connected(fc, num_outputs=final_dim, activation_fn=None,
                                                             scope='{}_gfc_final'.format(stage_idx))

    return fc_final  # cfeats: [pn,fc_dims+gxyz_dim+feats_dim]


def graph_conv_semantic_pool_v1(xyzs, dxyzs, pxyzs, feats, vlens, vbegs, vcens, voxel_size, block_size, reuse=False):
    with tf.name_scope('refine_graph_conv_edge_net'):
        with tf.variable_scope('refine_graph_conv_edge_net',reuse=reuse):
            feats=tf.contrib.layers.fully_connected(feats, num_outputs=256,scope='semantic_embed',
                                                    activation_fn=tf.nn.relu, reuse=reuse)
            with tf.name_scope('conv_stage0'):
                fc0, lf0 , _ = graph_conv_pool_stage_edge_new(0, xyzs, dxyzs, feats, 256, radius=0.1,
                                                              reuse=reuse, voxel_size=voxel_size,
                                                              gxyz_dim=16, gc_dims=[16,16],
                                                              gfc_dims=[128,128,128], final_dim=256)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens, vbegs)

            with tf.name_scope('conv_stage1'):
                fc1, lf1 , _ = graph_conv_pool_stage_edge_new(1, pxyzs, pxyzs, fc0_pool, 256, radius=1.5,
                                                              reuse=reuse, voxel_size=block_size,
                                                              gxyz_dim=16, gc_dims=[64,64,64,64],
                                                              gfc_dims=[128,128,128], final_dim=256)
                fc1_pool = tf.reduce_max(fc1, axis=0)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = tf.tile(tf.expand_dims(fc1_pool, axis=0), [tf.shape(fc1)[0], 1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens, vbegs, vcens)
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf=tf.concat([lf0,fc0],axis=1)

    return upf0,lf


def graph_conv_pool_block_edge_simp(xyzs, feats, stage_idx, layer_idx, ofn, ncens, nidxs, nlens, nbegs, reuse):
    feats = tf.contrib.layers.fully_connected(feats, num_outputs=ofn, scope='{}_{}_fc'.format(stage_idx, layer_idx),
                                              activation_fn=tf.nn.relu, reuse=reuse)
    feats = graph_conv_edge_simp(xyzs, feats, ofn, [ofn/2, ofn/2], [ofn/2, ofn/2], ofn, nidxs, nlens, nbegs, ncens,
                                 '{}_{}_gc'.format(stage_idx,layer_idx), reuse=reuse)
    return feats


def graph_conv_pool_block_edge_xyz_simp(sxyzs, stage_idx, gxyz_dim, ncens, nidxs, nlens, nbegs, reuse):
    xyz_gc=graph_conv_edge_xyz_simp(sxyzs, gxyz_dim, [gxyz_dim/2, gxyz_dim/2], [gxyz_dim/2, gxyz_dim/2], gxyz_dim,
                                    nidxs, nlens, nbegs, ncens, '{}_xyz_gc'.format(stage_idx), reuse=reuse)
    return xyz_gc


def graph_conv_pool_stage_edge_simp(stage_idx, xyzs, dxyz, feats, feats_dim, gxyz_dim, gc_dims, gfc_dims, final_dim,
                                    radius, voxel_size, reuse, xyz_fn=graph_conv_pool_block_edge_xyz_simp,
                                    feats_fn=graph_conv_pool_block_edge_simp):
    ops=[]
    with tf.name_scope('stage_{}'.format(stage_idx)):
        nidxs,nlens,nbegs,ncens=search_neighborhood(xyzs,radius)

        sxyzs = neighbor_ops.neighbor_scatter(xyzs, nidxs, nlens, nbegs, use_diff=True)  # [en,ifn]
        sxyzs /= radius   # rescale

        xyz_gc=xyz_fn(sxyzs,stage_idx,gxyz_dim,ncens,nidxs,nlens,nbegs,reuse)
        ops.append(xyz_gc)
        cfeats = tf.concat([xyz_gc, feats], axis=1)

        cdim = feats_dim + gxyz_dim
        conv_fn = partial(feats_fn, ncens=ncens, nidxs=nidxs, nlens=nlens, nbegs=nbegs, reuse=reuse)

        layer_idx = 1
        for gd in gc_dims:
            conv_feats = conv_fn(sxyzs, cfeats, stage_idx, layer_idx, gd)
            cfeats = tf.concat([cfeats, conv_feats], axis=1)
            ops.append(conv_feats)
            layer_idx += 1
            cdim += gd

        with framework.arg_scope([tf.contrib.layers.fully_connected], activation_fn=tf.nn.relu, reuse=reuse):
            with tf.name_scope('fc_global{}'.format(stage_idx)):
                dxyz= dxyz / voxel_size
                fc_feats = tf.concat([cfeats, dxyz], axis=1)
                for i, gfd in enumerate(gfc_dims):
                    fc = tf.contrib.layers.fully_connected(fc_feats, num_outputs=gfd,
                                                           scope='{}_{}_gfc'.format(stage_idx, i))
                    fc_feats=tf.concat([fc,fc_feats],axis=1)

                fc_final = tf.contrib.layers.fully_connected(fc_feats, num_outputs=final_dim, activation_fn=None,
                                                             scope='{}_final_gfc'.format(stage_idx))

    return fc_final, cfeats, ops  # cfeats: [pn,fc_dims+gxyz_dim+feats_dim]


def graph_conv_pool_edge_simp(xyzs, dxyzs, pxyzs, feats, vlens, vbegs, vcens, voxel_size, block_size, reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                # 8 64 64*2
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_simp(0, xyzs, dxyzs, feats, tf.shape(feats)[1], radius=0.1,
                                                                 reuse=reuse, voxel_size=voxel_size,
                                                                 gxyz_dim=16, gc_dims=[16,16,16,16,16,16],
                                                                 gfc_dims=[16,16,16], final_dim=128)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens, vbegs)

            with tf.name_scope('conv_stage1'):
                # 16 288 512*2
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_simp(1, pxyzs, pxyzs, fc0_pool, 128, radius=0.5,
                                                                 reuse=reuse, voxel_size=block_size,
                                                                 gxyz_dim=16, gc_dims=[32,32,32,32,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=512)
                fc1_pool = tf.reduce_max(fc1, axis=0)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = tf.tile(tf.expand_dims(fc1_pool, axis=0), [tf.shape(fc1)[0], 1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens, vbegs, vcens)
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf = tf.concat([fc0, lf0], axis=1)

            ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            ops0+=ops1
    # 1528 + 132
    return upf0, lf, ops0


def graph_conv_pool_edge_simp_2layers(xyzs, dxyzs, feats, vlens, vbegs, vcens, voxel_sizes, block_size,
                                      radius=(0.15,0.3,0.5), reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_simp(0, xyzs[0], dxyzs[0], feats, tf.shape(feats)[1], radius=radius[0],
                                                                 reuse=reuse, voxel_size=voxel_sizes[0],
                                                                 gxyz_dim=16, gc_dims=[16,16],
                                                                 gfc_dims=[8,8,8], final_dim=64)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens[0], vbegs[0])             # 64
                lf0_avg = graph_avg_pool_stage(0, lf0, vlens[0], vbegs[0], vcens[0])    # 61
                ifeats_0 = tf.concat([fc0_pool,lf0_avg],axis=1)

            with tf.name_scope('conv_stage1'):
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_simp(1, xyzs[1], xyzs[1], ifeats_0, tf.shape(ifeats_0)[1], radius=radius[1],
                                                                 reuse=reuse, voxel_size=voxel_sizes[1],
                                                                 gxyz_dim=16, gc_dims=[32,32,32,32,32,32,32,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=256)
                fc1_pool = graph_max_pool_stage(1, fc1, vlens[1], vbegs[1])         # 256
                lf1_avg = graph_avg_pool_stage(1, lf1, vlens[1], vbegs[1], vcens[1])# 429
                ifeats_1 = tf.concat([fc1_pool,lf1_avg],axis=1)                     # 685

            with tf.name_scope('conv_stage2'):
                fc2, lf2, ops2 = graph_conv_pool_stage_edge_simp(2, xyzs[2], xyzs[2], ifeats_1, tf.shape(ifeats_1)[1], radius=radius[2],
                                                                 reuse=reuse, voxel_size=block_size,
                                                                 gxyz_dim=16, gc_dims=[32,32,32,32,32,32,32,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=512)
                fc2_pool = tf.reduce_max(fc2, axis=0)
                lf2_avg = tf.reduce_mean(lf2, axis=0)
                ifeats_2 = tf.concat([fc2_pool,lf2_avg],axis=0)

            with tf.name_scope('unpool_stage2'):
                upfeats2 = tf.tile(tf.expand_dims(ifeats_2, axis=0), [tf.shape(fc2)[0], 1])
                upf2 = tf.concat([upfeats2, fc2, lf2], axis=1)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = graph_unpool_stage(1, upf2, vlens[1], vbegs[1], vcens[1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens[0], vbegs[0], vcens[0])
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf = tf.concat([fc0, lf0], axis=1)

            # ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            # ops0+=ops1
            ops=[fc0,lf0,fc1,lf1,fc2,lf2]

    return upf0, lf, ops


def graph_conv_pool_edge_simp_2layers_s3d(xyzs, dxyzs, feats, vlens, vbegs, vcens, voxel_sizes, block_size,
                                          radius=(0.15,0.3,0.5), reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_simp(0, xyzs[0], dxyzs[0], feats, tf.shape(feats)[1], radius=radius[0],
                                                                 reuse=reuse, voxel_size=voxel_sizes[0]/2.0,
                                                                 gxyz_dim=16, gc_dims=[16],
                                                                 gfc_dims=[16,16,16], final_dim=64)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens[0], vbegs[0])             # 64
                lf0_avg = graph_avg_pool_stage(0, lf0, vlens[0], vbegs[0], vcens[0])    # 61
                ifeats_0 = tf.concat([fc0_pool,lf0_avg],axis=1)

            with tf.name_scope('conv_stage1'):
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_simp(1, xyzs[1], xyzs[1], ifeats_0, tf.shape(ifeats_0)[1], radius=radius[1],
                                                                 reuse=reuse, voxel_size=voxel_sizes[1]/2.0,
                                                                 gxyz_dim=16, gc_dims=[16,16,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=128)
                fc1_pool = graph_max_pool_stage(1, fc1, vlens[1], vbegs[1])         # 256
                lf1_avg = graph_avg_pool_stage(1, lf1, vlens[1], vbegs[1], vcens[1])# 429
                ifeats_1 = tf.concat([fc1_pool,lf1_avg],axis=1)                     # 685

            with tf.name_scope('conv_stage2'):
                fc2, lf2, ops2 = graph_conv_pool_stage_edge_simp(2, xyzs[2], xyzs[2], ifeats_1, tf.shape(ifeats_1)[1], radius=radius[2],
                                                                 reuse=reuse, voxel_size=block_size/2.0,
                                                                 gxyz_dim=16, gc_dims=[32,32,64,64],
                                                                 gfc_dims=[64,64,64], final_dim=384)
                fc2_pool = tf.reduce_max(fc2, axis=0)
                lf2_avg = tf.reduce_mean(lf2, axis=0)
                ifeats_2 = tf.concat([fc2_pool,lf2_avg],axis=0)

            with tf.name_scope('unpool_stage2'):
                upfeats2 = tf.tile(tf.expand_dims(ifeats_2, axis=0), [tf.shape(fc2)[0], 1])
                upf2 = tf.concat([upfeats2, fc2, lf2], axis=1)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = graph_unpool_stage(1, upf2, vlens[1], vbegs[1], vcens[1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens[0], vbegs[0], vcens[0])
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf = tf.concat([fc0, lf0], axis=1)

            # ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            # ops0+=ops1
            ops=[fc0,lf0,fc1,lf1,fc2,lf2]

    return upf0, lf, ops



###############################

def graph_conv_pool_block_edge_simp_test(xyzs, feats, stage_idx, layer_idx, ofn, ncens, nidxs, nlens, nbegs, reuse):
    feats = tf.contrib.layers.fully_connected(feats, num_outputs=ofn, scope='{}_{}_fc'.format(stage_idx, layer_idx),
                                              activation_fn=tf.nn.relu, reuse=reuse)
    feats = graph_conv_edge_simp_test(xyzs, feats, ofn, [ofn/2, ofn/2], [ofn/2, ofn/2], ofn, nidxs, nlens, nbegs, ncens,
                                 '{}_{}_gc'.format(stage_idx,layer_idx), reuse=reuse)
    return feats


def graph_conv_pool_block_edge_xyz_simp_test(sxyzs, stage_idx, gxyz_dim, ncens, nidxs, nlens, nbegs, reuse):
    xyz_gc=graph_conv_edge_xyz_simp_test(sxyzs, gxyz_dim, [gxyz_dim/2, gxyz_dim/2], [gxyz_dim/2, gxyz_dim/2], gxyz_dim,
                                         nidxs, nlens, nbegs, ncens, '{}_xyz_gc'.format(stage_idx), reuse=reuse)
    return xyz_gc

def graph_conv_pool_edge_simp_2layers_test(xyzs, dxyzs, feats, vlens, vbegs, vcens, voxel_sizes, block_size,
                                           radius=(0.15,0.3,0.5), reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                # 8 64 64*2
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_simp(0, xyzs[0], dxyzs[0], feats, tf.shape(feats)[1], radius=radius[0],
                                                                 reuse=reuse, voxel_size=voxel_sizes[0],
                                                                 gxyz_dim=16, gc_dims=[16,16],
                                                                 gfc_dims=[8,8,8], final_dim=64,
                                                                 xyz_fn=graph_conv_pool_block_edge_xyz_simp_test,
                                                                 feats_fn=graph_conv_pool_block_edge_simp_test)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens[0], vbegs[0])
                lf0_avg = graph_avg_pool_stage(0, lf0, vlens[0], vbegs[0], vcens[0])
                ifeats_0 = tf.concat([fc0_pool,lf0_avg],axis=1)

            with tf.name_scope('conv_stage1'):
                # 16 288 512*2
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_simp(1, xyzs[1], xyzs[1], ifeats_0, tf.shape(ifeats_0)[1], radius=radius[1],
                                                                 reuse=reuse, voxel_size=voxel_sizes[1],
                                                                 gxyz_dim=16, gc_dims=[32,32,32,32,32,32,32,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=256,
                                                                 xyz_fn=graph_conv_pool_block_edge_xyz_simp_test,
                                                                 feats_fn=graph_conv_pool_block_edge_simp_test)
                fc1_pool = graph_max_pool_stage(1, fc1, vlens[1], vbegs[1])
                lf1_avg = graph_avg_pool_stage(1, lf1, vlens[1], vbegs[1], vcens[1])
                ifeats_1 = tf.concat([fc1_pool,lf1_avg],axis=1)

            with tf.name_scope('conv_stage2'):
                # 16 288 512*2
                fc2, lf2, ops2 = graph_conv_pool_stage_edge_simp(2, xyzs[2], xyzs[2], ifeats_1, tf.shape(ifeats_1)[1], radius=radius[2],
                                                                 reuse=reuse, voxel_size=block_size,
                                                                 gxyz_dim=16, gc_dims=[32,32,32,32,32,32,32,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=512,
                                                                 xyz_fn=graph_conv_pool_block_edge_xyz_simp_test,
                                                                 feats_fn=graph_conv_pool_block_edge_simp_test)
                fc2_pool = tf.reduce_max(fc2, axis=0)
                lf2_avg = tf.reduce_mean(lf2, axis=0)
                ifeats_2 = tf.concat([fc2_pool,lf2_avg],axis=0)

            with tf.name_scope('unpool_stage2'):
                upfeats2 = tf.tile(tf.expand_dims(ifeats_2, axis=0), [tf.shape(fc2)[0], 1])
                upf2 = tf.concat([upfeats2, fc2, lf2], axis=1)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = graph_unpool_stage(1, upf2, vlens[1], vbegs[1], vcens[1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens[0], vbegs[0], vcens[0])
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf = tf.concat([fc0, lf0], axis=1)

            # ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            # ops0+=ops1

    # 1528 + 132
    return upf0, lf, ops0


###################################


def graph_conv_pool_edge_simp_2layers_v2(xyzs, dxyzs, feats, vlens, vbegs, vcens, voxel_sizes, block_size, reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_simp(0, xyzs[0], dxyzs[0], feats, tf.shape(feats)[1], radius=0.15,
                                                                 reuse=reuse, voxel_size=voxel_sizes[0],
                                                                 gxyz_dim=16, gc_dims=[16,16],
                                                                 gfc_dims=[8,8,8], final_dim=64)
                fc0_pool = graph_max_pool_stage(0, fc0, vlens[0], vbegs[0])

            with tf.name_scope('conv_stage1'):
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_simp(1, xyzs[1], xyzs[1], fc0_pool, tf.shape(fc0_pool)[1], radius=0.3,
                                                                 reuse=reuse, voxel_size=voxel_sizes[1],
                                                                 gxyz_dim=16, gc_dims=[32,32,32,32,32,32,32,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=256)
                fc1_pool = graph_max_pool_stage(1, fc1, vlens[1], vbegs[1])

            with tf.name_scope('conv_stage2'):
                fc2, lf2, ops2 = graph_conv_pool_stage_edge_simp(2, xyzs[2], xyzs[2], fc1_pool, tf.shape(fc1_pool)[1], radius=0.5,
                                                                 reuse=reuse, voxel_size=block_size,
                                                                 gxyz_dim=16, gc_dims=[32,32,32,32,32,32,32,32,32],
                                                                 gfc_dims=[32,32,32], final_dim=512)
                fc2_pool = tf.reduce_max(fc2, axis=0)
                lf2_avg = tf.reduce_mean(lf2, axis=0)
                ifeats_2 = tf.concat([fc2_pool,lf2_avg],axis=0)

            with tf.name_scope('unpool_stage2'):
                upfeats2 = tf.tile(tf.expand_dims(ifeats_2, axis=0), [tf.shape(fc2)[0], 1])
                upf2 = tf.concat([upfeats2, fc2, lf2], axis=1)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = graph_unpool_stage(1, upf2, vlens[1], vbegs[1], vcens[1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens[0], vbegs[0], vcens[0])
                upf0 = tf.concat([upfeats0, fc0, lf0], axis=1)

            lf = tf.concat([fc0, lf0], axis=1)

            # ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            # ops0+=ops1

    # 1528 + 132
    return upf0, lf, ops0




def graph_conv_pool_block_edge_simp_v2(xyzs, feats, stage_idx, layer_idx, ofn, ncens, nidxs, nlens, nbegs, reuse):
    feats = tf.contrib.layers.fully_connected(feats, num_outputs=ofn*2, scope='{}_{}_fc'.format(stage_idx, layer_idx),
                                              activation_fn=tf.nn.relu, reuse=reuse)
    feats = graph_conv_edge_simp(xyzs, feats, ofn*2, [ofn/2, ofn/2], [ofn, ofn], ofn, nidxs, nlens, nbegs, ncens,
                                 '{}_{}_gc'.format(stage_idx,layer_idx), reuse=reuse)
    return feats


def graph_conv_pool_block_edge_xyz_simp_v2(sxyzs, stage_idx, gxyz_dim, ncens, nidxs, nlens, nbegs, reuse):
    xyz_gc=graph_conv_edge_xyz_simp(sxyzs, gxyz_dim*2, [gxyz_dim, gxyz_dim], [gxyz_dim, gxyz_dim], gxyz_dim,
                                    nidxs, nlens, nbegs, ncens, '{}_xyz_gc'.format(stage_idx), reuse=reuse)
    return xyz_gc



def graph_conv_pool_stage_edge_simp_v2(stage_idx, xyzs, dxyz, feats, feats_dim, gxyz_dim, gc_dims, gfc_dims, final_dim,
                                       radius, voxel_size, reuse):
    ops=[]
    with tf.name_scope('stage_{}'.format(stage_idx)):
        nidxs,nlens,nbegs,ncens=search_neighborhood(xyzs,radius)

        sxyzs = neighbor_ops.neighbor_scatter(xyzs, nidxs, nlens, nbegs, use_diff=True)  # [en,ifn]
        sxyzs /= radius   # rescale

        xyz_gc=graph_conv_pool_block_edge_xyz_simp_v2(sxyzs,stage_idx,gxyz_dim,ncens,nidxs,nlens,nbegs,reuse)
        ops.append(xyz_gc)
        cfeats = tf.concat([xyz_gc, feats], axis=1)

        cdim = feats_dim + gxyz_dim
        conv_fn = partial(graph_conv_pool_block_edge_simp_v2, ncens=ncens, nidxs=nidxs, nlens=nlens, nbegs=nbegs, reuse=reuse)

        layer_idx = 1
        for gd in gc_dims:
            conv_feats = conv_fn(sxyzs, cfeats, stage_idx, layer_idx, gd)
            cfeats = tf.concat([cfeats, conv_feats], axis=1)
            ops.append(conv_feats)
            layer_idx += 1
            cdim += gd

        with framework.arg_scope([tf.contrib.layers.fully_connected], activation_fn=tf.nn.relu, reuse=reuse):
            with tf.name_scope('fc_global{}'.format(stage_idx)):
                dxyz= dxyz / voxel_size
                fc_feats = tf.concat([cfeats, dxyz], axis=1)
                for i, gfd in enumerate(gfc_dims):
                    fc = tf.contrib.layers.fully_connected(fc_feats, num_outputs=gfd,
                                                           scope='{}_{}_gfc'.format(stage_idx, i))
                    fc_feats=tf.concat([fc,fc_feats],axis=1)

                fc_final = tf.contrib.layers.fully_connected(fc_feats, num_outputs=final_dim, activation_fn=None,
                                                             scope='{}_final_gfc'.format(stage_idx))

    return fc_final, cfeats, ops  # cfeats: [pn,fc_dims+gxyz_dim+feats_dim]


def graph_conv_pool_edge_simp_v2(xyzs, dxyzs, pxyzs, feats, vlens, vbegs, vcens, voxel_size, block_size, reuse=False):
    with tf.name_scope('base_graph_conv_edge_net'):
        with tf.variable_scope('base_graph_conv_edge_net',reuse=reuse):
            with tf.name_scope('conv_stage0'):
                # 8 64 64*2
                fc0, lf0, ops0 = graph_conv_pool_stage_edge_simp_v2(0, xyzs, dxyzs, feats, tf.shape(feats)[1], radius=0.1,
                                                                    reuse=reuse, voxel_size=voxel_size,
                                                                    gxyz_dim=16, gc_dims=[16,16,16],
                                                                    gfc_dims=[16,16,16], final_dim=128)
                feats0=tf.concat([fc0,lf0],axis=1)
                fc2, lf2, ops2 = graph_conv_pool_stage_edge_simp(2, xyzs, dxyzs, feats0, tf.shape(feats0)[1], radius=0.2,
                                                                 reuse=reuse, voxel_size=voxel_size,
                                                                 gxyz_dim=16, gc_dims=[16,16,16],
                                                                 gfc_dims=[16,16,16], final_dim=256)

                fc2_pool = graph_max_pool_stage(0, fc2, vlens, vbegs)

            with tf.name_scope('conv_stage1'):
                # 16 288 512*2
                fc1, lf1, ops1 = graph_conv_pool_stage_edge_simp_v2(1, pxyzs, pxyzs, fc2_pool, 128, radius=0.5,
                                                                 reuse=reuse, voxel_size=block_size,
                                                                 gxyz_dim=16, gc_dims=[32,32,32,64,64,64],
                                                                 gfc_dims=[32,32,32], final_dim=512)
                fc1_pool = tf.reduce_max(fc1, axis=0)

            with tf.name_scope('unpool_stage1'):
                upfeats1 = tf.tile(tf.expand_dims(fc1_pool, axis=0), [tf.shape(fc1)[0], 1])
                upf1 = tf.concat([upfeats1, fc1, lf1], axis=1)

            with tf.name_scope('unpool_stage0'):
                upfeats0 = graph_unpool_stage(0, upf1, vlens, vbegs, vcens)
                upf0 = tf.concat([upfeats0, fc2, lf2], axis=1)

            lf = tf.concat([fc2, lf2], axis=1)

            ops1=[graph_unpool_stage(1+idx, op, vlens, vbegs, vcens) for idx,op in enumerate(ops1)]
            ops0+=ops1
    # 1528 + 132
    return upf0, lf, ops0


def test_model():
    num_classes = 13
    from io_util import read_pkl,get_block_train_test_split
    import numpy as np
    import random
    import time
    train_list,test_list=get_block_train_test_split()
    random.shuffle(train_list)
    cxyzs, dxyzs, rgbs, covars, lbls, vlens, vlens_bgs, vcidxs, cidxs, nidxs, nidxs_bgs, nidxs_lens, block_mins = \
        read_pkl('data/S3DIS/sampled_train/{}'.format(train_list[0]))

    xyzs_pl = tf.placeholder(tf.float32, [None, 3], 'xyzs')
    feats_pl = tf.placeholder(tf.float32, [None, 12], 'feats')
    labels_pl = tf.placeholder(tf.int32, [None], 'labels')

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    config.log_device_placement = False
    with tf.Session(config=config) as sess:
        xyzs, pxyzs, dxyzs, feats, labels, vlens, vbegs, vcens = points_pooling(xyzs_pl,feats_pl,labels_pl,0.3,3.0)
        global_feats,local_feats,_=graph_conv_pool_edge_new(xyzs,dxyzs,pxyzs,feats,vlens,vbegs,vcens,False)
        global_feats = tf.expand_dims(global_feats, axis=0)
        local_feats = tf.expand_dims(local_feats, axis=0)
        logits = classifier_v3(global_feats, local_feats, tf.Variable(False,trainable=False,dtype=tf.bool),
                               num_classes, False, use_bn=False)

        labels = tf.cast(labels, tf.int64)
        flatten_logits = tf.reshape(logits, [-1, num_classes])  # [pn,num_classes]
        acc=tf.reduce_mean(tf.cast(tf.equal(tf.argmax(flatten_logits,axis=1),labels),tf.float32),axis=0)

        loss=tf.losses.sparse_softmax_cross_entropy(labels,flatten_logits)
        opt=tf.train.GradientDescentOptimizer(1e-2)
        train_op=opt.minimize(loss)

        sess.run(tf.global_variables_initializer())

        for k in xrange(20):
            bg=time.time()
            options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()

            _,acc_val=sess.run([train_op,acc],feed_dict={
                xyzs_pl:cxyzs[0][0],
                feats_pl:np.concatenate([rgbs[0],covars[0]],axis=1),
                labels_pl:lbls[0]
            },options=options, run_metadata=run_metadata)
            fetched_timeline = timeline.Timeline(run_metadata.step_stats)
            chrome_trace = fetched_timeline.generate_chrome_trace_format()
            with open('timeline.json', 'w') as f:
                f.write(chrome_trace)
            print 'cost {} {} s'.format(time.time()-bg,1/(time.time()-bg))
            print acc_val

def test_block():

    from io_util import read_pkl,get_block_train_test_split
    import numpy as np
    from draw_util import output_points,get_class_colors
    import random
    train_list,test_list=get_block_train_test_split()
    random.shuffle(train_list)
    cxyzs, dxyzs, rgbs, covars, lbls, vlens, vlens_bgs, vcidxs, cidxs, nidxs, nidxs_bgs, nidxs_lens, block_mins = \
        read_pkl('data/S3DIS/sampled_train/{}'.format(train_list[0]))

    xyzs_pl = tf.placeholder(tf.float32, [None, 3], 'xyzs')
    feats_pl = tf.placeholder(tf.float32, [None, 3], 'feats')
    labels_pl = tf.placeholder(tf.int32, [None], 'labels')
    xyzs_op, pxyzs_op, dxyzs_op, feats_op, labels_op, vlens_op, vbegs_op, vcens_op = class_pooling(xyzs_pl, feats_pl, labels_pl,
                                                                                                   labels_pl, 0.5, 3.0)
    nidxs_op, nlens_op, nbegs_op, ncens_op=search_neighborhood(xyzs_pl,0.1)


    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    config.log_device_placement = False
    with tf.Session(config=config) as sess:
        for t in xrange(10):
            # for l in xrange(10):
                # options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                # run_metadata = tf.RunMetadata()

            xyzs, pxyzs, dxyzs, feats, labels, vlens, vbegs, vcens, nidxs=\
                sess.run([xyzs_op, pxyzs_op, dxyzs_op, feats_op, labels_op, vlens_op, vbegs_op, vcens_op, nidxs_op],
                         feed_dict={xyzs_pl:cxyzs[t][0],feats_pl:rgbs[t],labels_pl:lbls[t]},)
                             # options=options, run_metadata=run_metadata)

            #     fetched_timeline = timeline.Timeline(run_metadata.step_stats)
            #     chrome_trace = fetched_timeline.generate_chrome_trace_format()
            #     with open('timeline.json', 'w') as f:
            #         f.write(chrome_trace)
            #
            # exit(0)

            print 'before avg {} after {}'.format(np.mean(nidxs_lens[t][0]),len(nidxs)/float(len(xyzs)))

            colors = np.random.randint(0, 256, [len(vlens), 3])
            pcolors = []
            for c, l in zip(colors, vlens):
                pcolors += [c for _ in xrange(l)]

            pcolors = np.asarray(pcolors, np.int32)
            output_points('test_result/before{}.txt'.format(t), xyzs, pcolors)
            output_points('test_result/after{}.txt'.format(t), pxyzs, colors)
            output_points('test_result/colors{}.txt'.format(t), xyzs, feats*127+128)

            colors=get_class_colors()
            output_points('test_result/labels{}.txt'.format(t), xyzs, colors[labels.flatten(),:])

            # test begs
            cur_len = 0
            for i in xrange(len(vlens)):
                assert cur_len == vbegs[i]
                cur_len += vlens[i]

            # test dxyzs
            for i in xrange(len(vlens)):
                bg = vbegs[i]
                ed = bg + vlens[i]
                dxyzs[bg:ed] += pxyzs[i]

            print 'diff max {} mean {} sum {}'.format(np.max(dxyzs - xyzs), np.mean(dxyzs - xyzs), np.sum(dxyzs - xyzs))

            print 'pn {} mean voxel pn {} voxel num {}'.format(len(dxyzs),np.mean(vlens),len(vlens))


def check_vidxs(max_cens,max_len,lens,begs,cens):
    nbegs=np.cumsum(lens)
    assert np.sum(nbegs[:-1]!=begs[1:])==0
    assert begs[0]==0

    assert np.sum(cens>=max_cens)==0
    assert max_len==lens[-1]+begs[-1]


def output_hierarchy(pts1,pts2,cens,name):
    colors=np.random.randint(0,256,[len(pts2),3])
    output_points('test_result/{}_dense.txt'.format(name),pts1,colors[cens,:])
    output_points('test_result/{}_sparse.txt'.format(name),pts2,colors)


def check_dxyzs(pts1,pts2,dpts1,vcens):
    pn1=pts1.shape[0]
    tmp_dpts1=np.copy(dpts1)
    for i in xrange(pn1):
        tmp_dpts1[i]+=pts2[vcens[i]]

    print np.mean(np.abs(tmp_dpts1-pts1),axis=0),np.max(np.abs(tmp_dpts1-pts1),axis=0)

def check_nn(pn,idxs,lens,begs,cens):
    assert begs[-1]+lens[-1]==len(idxs)
    assert np.sum(idxs>=pn)==0
# nr1 = 0.125
# nr2 = 0.5
# nr3 = 2.0
# vc1 = 0.25
# vc2 = 1.0
def test_semantic3d_block():

    from io_util import read_pkl,get_semantic3d_block_train_list
    import numpy as np
    import random
    train_list,test_list=get_semantic3d_block_train_list()
    train_list=['data/Semantic3D.Net/block/sampled/merged/'+fn for fn in train_list]
    test_list=['data/Semantic3D.Net/block/sampled/merged/'+fn for fn in test_list]
    train_list+=test_list
    random.shuffle(train_list)

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    config.log_device_placement = False
    sess=tf.Session(config=config)
    # for k in xrange(len(train_list)):
    for k in xrange(3):
    # for _ in xrange(1):
        xyzs,rgbs,covars,labels=read_pkl(train_list[k])
        # xyzs,rgbs,covars,labels=read_pkl('tmp_data.pkl')

        xyzs_pl = tf.placeholder(tf.float32, [None, 3], 'xyzs')
        feats_pl = tf.placeholder(tf.float32, [None, 4], 'feats')
        labels_pl = tf.placeholder(tf.int32, [None], 'labels')
        [pts1, pts2, pts3], [dpts1, dpts2], feats, _, [vlens1, vlens2], [vbegs1, vbegs2], [vcens1, vcens2], vidx1, vidx2 = \
            points_pooling_two_layers_tmp(xyzs_pl, feats_pl, labels_pl, 0.25, 1.0, 10.0)

        nidxs1, nlens1, nbegs1, ncens1 = search_neighborhood(pts1, 0.25)
        nidxs2, nlens2, nbegs2, ncens2 = search_neighborhood(pts2, 0.5)
        nidxs3, nlens3, nbegs3, ncens3 = search_neighborhood(pts3, 2.0)

        for t in xrange(len(xyzs)):
            xyzs1,xyzs2,xyzs3,nn1,nn2,nn3,vi1,vi2,vl1,vl2,vc1,vc2,vb1,vb2,\
                nl1,nl2,nl3,nb1,nb2,nb3,nc1,nc2,nc3=\
                sess.run([pts1, pts2, pts3, nidxs1, nidxs2, nidxs3, vidx1, vidx2,
                          vlens1, vlens2,vcens1, vcens2,vbegs1, vbegs2,
                          nlens1,nlens2,nlens3,nbegs1,nbegs2,nbegs3,ncens1,ncens2,ncens3],
                         feed_dict={xyzs_pl:xyzs[t],feats_pl:rgbs[t],labels_pl:labels[t]},)
            assert np.sum(vi1<0)==0
            assert np.sum(vi2<0)==0
            check_vidxs(len(xyzs2),len(xyzs1),vl1,vb1,vc1)
            check_vidxs(len(xyzs3),len(xyzs2),vl2,vb2,vc2)
            # check_vidxs(len(xyzs1),len(nc1),nl1,nb1,nc1)
            # check_vidxs(len(xyzs2),len(nc2),nl2,nb2,nc2)
            # check_vidxs(len(xyzs3),len(nc3),nl3,nb3,nc3)

            print '{}_lvl 0 pn {} avg_nn {}'.format(t,len(xyzs1),len(nn1)/float(len(xyzs1)))
            print '{}_lvl 1 pn {} avg_nn {}'.format(t,len(xyzs2),len(nn2)/float(len(xyzs2)))
            print '{}_lvl 2 pn {} avg_nn {}'.format(t,len(xyzs3),len(nn3)/float(len(xyzs3)))
            print np.min(vi1,axis=0)
            print np.max(vi1,axis=0)
            print np.min(vi2,axis=0)
            print np.max(vi2,axis=0)

            # print vc1[466]
            # print vb1[vc1[466]],vl1[vc1[466]]
            # for k in xrange(466,475):
            #     print nl1[k]
            # print xyzs2[vc1[466]]
            # print nl2[vc1[466]]
            # output_points('test_result/{}.txt'.format(),xyzs1[466:475,:])

            print '/////////////////////////////'


        print '{} done'.format(train_list[k])



if __name__=="__main__":
    test_semantic3d_block()