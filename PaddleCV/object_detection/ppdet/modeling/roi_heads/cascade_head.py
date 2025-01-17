# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import paddle.fluid as fluid
from paddle.fluid.param_attr import ParamAttr
from paddle.fluid.initializer import Normal, Xavier
from paddle.fluid.regularizer import L2Decay

from ppdet.modeling.ops import MultiClassNMS
from ppdet.core.workspace import register

__all__ = ['CascadeBBoxHead']


@register
class CascadeBBoxHead(object):
    """
    Cascade RCNN bbox head

    Args:
        head (object): the head module instance
        nms (object): `MultiClassNMS` instance
        num_classes: number of output classes
    """
    __inject__ = ['head', 'nms']

    def __init__(self, head, nms=MultiClassNMS().__dict__, num_classes=81):
        super(CascadeBBoxHead, self).__init__()
        self.head = head
        self.nms = nms
        self.num_classes = num_classes
        if isinstance(nms, dict):
            self.nms = MultiClassNMS(**nms)

    def get_output(self,
                   roi_feat,
                   cls_agnostic_bbox_reg=2,
                   wb_scalar=2.0,
                   name=''):
        """
        Get bbox head output.

        Args:
            roi_feat (Variable): RoI feature from RoIExtractor.
            cls_agnostic_bbox_reg(Int): BBox regressor are class agnostic.
            wb_scalar(Float): Weights and Bias's learning rate.
            name(String): Layer's name

        Returns:
            cls_score(Variable): cls score.
            bbox_pred(Variable): bbox regression.
        """
        head_feat = self.head(roi_feat, wb_scalar, name)
        cls_score = fluid.layers.fc(input=head_feat,
                                    size=self.num_classes,
                                    act=None,
                                    name='cls_score' + name,
                                    param_attr=ParamAttr(
                                        name='cls_score%s_w' % name,
                                        initializer=Normal(
                                            loc=0.0, scale=0.01),
                                        learning_rate=wb_scalar),
                                    bias_attr=ParamAttr(
                                        name='cls_score%s_b' % name,
                                        learning_rate=wb_scalar,
                                        regularizer=L2Decay(0.)))
        bbox_pred = fluid.layers.fc(input=head_feat,
                                    size=4 * cls_agnostic_bbox_reg,
                                    act=None,
                                    name='bbox_pred' + name,
                                    param_attr=ParamAttr(
                                        name='bbox_pred%s_w' % name,
                                        initializer=Normal(
                                            loc=0.0, scale=0.001),
                                        learning_rate=wb_scalar),
                                    bias_attr=ParamAttr(
                                        name='bbox_pred%s_b' % name,
                                        learning_rate=wb_scalar,
                                        regularizer=L2Decay(0.)))
        return cls_score, bbox_pred

    def get_loss(self, rcnn_pred_list, rcnn_target_list, rcnn_loss_weight_list):
        """
        Get bbox_head loss.

        Args:
            rcnn_pred_list(List): Cascade RCNN's head's output including
                bbox_pred and cls_score
            rcnn_target_list(List): Cascade rcnn's bbox and label target
            rcnn_loss_weight_list(List): The weight of location and class loss

        Return:
            loss_cls(Variable): bbox_head loss.
            loss_bbox(Variable): bbox_head loss.
        """
        loss_dict = {}
        for i, (rcnn_pred, rcnn_target
                ) in enumerate(zip(rcnn_pred_list, rcnn_target_list)):
            labels_int64 = fluid.layers.cast(x=rcnn_target[1], dtype='int64')
            labels_int64.stop_gradient = True

            loss_cls = fluid.layers.softmax_with_cross_entropy(
                logits=rcnn_pred[0],
                label=labels_int64,
                numeric_stable_mode=True, )
            loss_cls = fluid.layers.reduce_mean(
                loss_cls, name='loss_cls_' + str(i)) * rcnn_loss_weight_list[i]

            loss_bbox = fluid.layers.smooth_l1(
                x=rcnn_pred[1],
                y=rcnn_target[2],
                inside_weight=rcnn_target[3],
                outside_weight=rcnn_target[4],
                sigma=1.0,  # detectron use delta = 1./sigma**2
            )
            loss_bbox = fluid.layers.reduce_mean(
                loss_bbox,
                name='loss_bbox_' + str(i)) * rcnn_loss_weight_list[i]

            loss_dict['loss_cls_%d' % i] = loss_cls
            loss_dict['loss_loc_%d' % i] = loss_bbox

        return loss_dict

    def get_prediction(self,
                       im_info,
                       roi_feat_list,
                       rcnn_pred_list,
                       proposal_list,
                       cascade_bbox_reg_weights,
                       cls_agnostic_bbox_reg=2):
        """
        Get prediction bounding box in test stage.
        :
        Args:
            im_info (Variable): A 2-D LoDTensor with shape [B, 3]. B is the
                number of input images, each element consists
                of im_height, im_width, im_scale.
            rois_feat_list (List): RoI feature from RoIExtractor.
            rcnn_pred_list (Variable): Cascade rcnn's head's output
                including bbox_pred and cls_score
            proposal_list (List): RPN proposal boxes.
            cascade_bbox_reg_weights (List): BBox decode var.
            cls_agnostic_bbox_reg(Int): BBox regressor are class agnostic

        Returns:
            pred_result(Variable): Prediction result with shape [N, 6]. Each
               row has 6 values: [label, confidence, xmin, ymin, xmax, ymax].
               N is the total number of prediction.
        """
        self.im_scale = fluid.layers.slice(im_info, [1], starts=[2], ends=[3])
        boxes_cls_prob_l = []

        rcnn_pred = rcnn_pred_list[-1]  # stage 3
        repreat_num = 1
        repreat_num = 3
        bbox_reg_w = cascade_bbox_reg_weights[-1]
        for i in range(repreat_num):
            # cls score
            if i < 2:
                cls_score = self._head_share(
                    roi_feat_list[-1],  # roi_feat_3
                    name='_' + str(i + 1) if i > 0 else '')
            else:
                cls_score = rcnn_pred[0]
            cls_prob = fluid.layers.softmax(cls_score, use_cudnn=False)
            boxes_cls_prob_l.append(cls_prob)

        boxes_cls_prob_mean = (
            boxes_cls_prob_l[0] + boxes_cls_prob_l[1] + boxes_cls_prob_l[2]
        ) / 3.0

        # bbox pred
        proposals_boxes = proposal_list[-1]
        im_scale_lod = fluid.layers.sequence_expand(self.im_scale,
                                                    proposals_boxes)
        proposals_boxes = proposals_boxes / im_scale_lod
        bbox_pred = rcnn_pred[1]
        bbox_pred_new = fluid.layers.reshape(bbox_pred,
                                             (-1, cls_agnostic_bbox_reg, 4))
        if cls_agnostic_bbox_reg == 2:
            # only use fg box delta to decode box
            bbox_pred_new = fluid.layers.slice(
                bbox_pred_new, axes=[1], starts=[1], ends=[2])
            bbox_pred_new = fluid.layers.expand(bbox_pred_new, [1, 81, 1])
        decoded_box = fluid.layers.box_coder(
            prior_box=proposals_boxes,
            prior_box_var=bbox_reg_w,
            target_box=bbox_pred_new,
            code_type='decode_center_size',
            box_normalized=False,
            axis=1)

        # TODO: notice detectron use img.shape
        box_out = fluid.layers.box_clip(input=decoded_box, im_info=im_info)

        pred_result = self.nms(bboxes=box_out, scores=boxes_cls_prob_mean)
        return {"bbox": pred_result}

    def _head_share(self, roi_feat, wb_scalar=2.0, name=''):
        # FC6 FC7
        fan = roi_feat[1] * roi_feat[2] * roi_feat[3]
        fc6 = fluid.layers.fc(input=roi_feat,
                              size=self.head.num_chan,
                              act='relu',
                              name='fc6' + name,
                              param_attr=ParamAttr(
                                  name='fc6%s_w' % name,
                                  initializer=Xavier(fan_out=fan),
                                  learning_rate=wb_scalar, ),
                              bias_attr=ParamAttr(
                                  name='fc6%s_b' % name,
                                  learning_rate=2.0,
                                  regularizer=L2Decay(0.)))
        fc7 = fluid.layers.fc(input=fc6,
                              size=self.head.num_chan,
                              act='relu',
                              name='fc7' + name,
                              param_attr=ParamAttr(
                                  name='fc7%s_w' % name,
                                  initializer=Xavier(),
                                  learning_rate=wb_scalar, ),
                              bias_attr=ParamAttr(
                                  name='fc7%s_b' % name,
                                  learning_rate=2.0,
                                  regularizer=L2Decay(0.)))
        cls_score = fluid.layers.fc(input=fc7,
                                    size=self.num_classes,
                                    act=None,
                                    name='cls_score' + name,
                                    param_attr=ParamAttr(
                                        name='cls_score%s_w' % name,
                                        initializer=Normal(
                                            loc=0.0, scale=0.01),
                                        learning_rate=wb_scalar, ),
                                    bias_attr=ParamAttr(
                                        name='cls_score%s_b' % name,
                                        learning_rate=2.0,
                                        regularizer=L2Decay(0.)))
        return cls_score


@register
class FC6FC7Head(object):
    """
    Cascade RCNN head with two Fully Connected layers

    Args:
        num_chan (int): num of filters for the fc layers
    """

    def __init__(self, num_chan):
        super(FC6FC7Head, self).__init__()
        self.num_chan = num_chan

    def __call__(self, roi_feat, wb_scalar=1.0, name=''):
        fan = roi_feat[1] * roi_feat[2] * roi_feat[3]
        fc6 = fluid.layers.fc(input=roi_feat,
                              size=self.num_chan,
                              act='relu',
                              name='fc6' + name,
                              param_attr=ParamAttr(
                                  name='fc6%s_w' % name,
                                  initializer=Xavier(fan_out=fan),
                                  learning_rate=wb_scalar),
                              bias_attr=ParamAttr(
                                  name='fc6%s_b' % name,
                                  learning_rate=wb_scalar,
                                  regularizer=L2Decay(0.)))
        head_feat = fluid.layers.fc(input=fc6,
                                    size=self.num_chan,
                                    act='relu',
                                    name='fc7' + name,
                                    param_attr=ParamAttr(
                                        name='fc7%s_w' % name,
                                        initializer=Xavier(),
                                        learning_rate=wb_scalar),
                                    bias_attr=ParamAttr(
                                        name='fc7%s_b' % name,
                                        learning_rate=wb_scalar,
                                        regularizer=L2Decay(0.)))
        return head_feat
