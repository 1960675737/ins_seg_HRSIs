import logging
import numpy as np
import numpy.random as npr

from core.config import cfg
import roi_data.data_utils as data_utils
import utils.blob as blob_utils
import utils.boxes as box_utils
import torch
logger = logging.getLogger(__name__)


def get_rpn_blob_names(is_training=True):
    """Blob names used by RPN."""
    # im_info: (height, width, image scale)
    blob_names = ['im_info']
    if is_training:
        # gt boxes: (batch_idx, x1, y1, x2, y2, cls)
        blob_names += ['roidb']
        if cfg.FPN.FPN_ON and cfg.FPN.MULTILEVEL_RPN:
            # Same format as RPN blobs, but one per FPN level
            for lvl in range(cfg.FPN.RPN_MIN_LEVEL, cfg.FPN.RPN_MAX_LEVEL + 1):
                blob_names += [
                    'rpn_labels_int32_wide_fpn' + str(lvl),
                    'rpn_bbox_targets_wide_fpn' + str(lvl),
                    'rpn_bbox_inside_weights_wide_fpn' + str(lvl),
                    'rpn_bbox_outside_weights_wide_fpn' + str(lvl)
                ]
        else:
            # Single level RPN blobs
            blob_names += [
                'rpn_labels_int32_wide',
                'rpn_bbox_targets_wide',
                'rpn_bbox_inside_weights_wide',
                'rpn_bbox_outside_weights_wide'
            ]
    return blob_names


def add_rpn_blobs(blobs, im_scales, roidb):
    """Add blobs needed training RPN-only and end-to-end Faster R-CNN models."""
    if cfg.FPN.FPN_ON and cfg.FPN.MULTILEVEL_RPN:
        # RPN applied to many feature levels, as in the FPN paper
        k_max = cfg.FPN.RPN_MAX_LEVEL
        k_min = cfg.FPN.RPN_MIN_LEVEL
        foas = []
        for lvl in range(k_min, k_max + 1):
            field_stride = 2.**lvl
            anchor_sizes = (cfg.FPN.RPN_ANCHOR_START_SIZE * 2.**(lvl - k_min), )
            anchor_aspect_ratios = cfg.FPN.RPN_ASPECT_RATIOS
            foa = data_utils.get_field_of_anchors(
                field_stride, anchor_sizes, anchor_aspect_ratios
            )
            foas.append(foa)
        all_anchors = np.concatenate([f.field_of_anchors for f in foas])
    else:
        foa = data_utils.get_field_of_anchors(cfg.RPN.STRIDE, cfg.RPN.SIZES,
                                              cfg.RPN.ASPECT_RATIOS)
        all_anchors = foa.field_of_anchors

    for im_i, entry in enumerate(roidb):
        scale = im_scales[im_i]
        im_height = np.round(entry['height'] * scale)
        im_width = np.round(entry['width'] * scale)
        gt_inds = np.where(
            (entry['gt_classes'] > 0) & (entry['is_crowd'] == 0)
        )[0]
        gt_rois = entry['boxes'][gt_inds, :] * scale
        # TODO(rbg): gt_boxes is poorly named;
        # should be something like 'gt_rois_info'
        gt_boxes = blob_utils.zeros((len(gt_inds), 6))
        gt_boxes[:, 0] = im_i  # batch inds
        gt_boxes[:, 1:5] = gt_rois
        gt_boxes[:, 5] = entry['gt_classes'][gt_inds]
        im_info = np.array([[im_height, im_width, scale]], dtype=np.float32)
        blobs['im_info'].append(im_info)

        # Add RPN targets
        if cfg.FPN.FPN_ON and cfg.FPN.MULTILEVEL_RPN:
            # RPN applied to many feature levels, as in the FPN paper
            rpn_blobs = _get_rpn_blobs(
                im_height, im_width, foas, all_anchors, gt_rois
            )
            for i, lvl in enumerate(range(k_min, k_max + 1)):
                for k, v in rpn_blobs[i].items():
                    blobs[k + '_fpn' + str(lvl)].append(v)
        else:
            # Classical RPN, applied to a single feature level
            rpn_blobs = _get_rpn_blobs(
                im_height, im_width, [foa], all_anchors, gt_rois
            )
            for k, v in rpn_blobs.items():
                blobs[k].append(v)

    for k, v in blobs.items():
        if isinstance(v, list) and len(v) > 0:
            blobs[k] = np.concatenate(v)

    valid_keys = [
        'has_visible_keypoints', 'boxes', 'segms', 'seg_areas', 'gt_classes',
        'gt_overlaps', 'is_crowd', 'box_to_gt_ind_map', 'gt_keypoints'
    ]
    minimal_roidb = [{} for _ in range(len(roidb))]
    for i, e in enumerate(roidb):
        for k in valid_keys:
            if k in e:
                minimal_roidb[i][k] = e[k]
    # blobs['roidb'] = blob_utils.serialize(minimal_roidb)
    blobs['roidb'] = minimal_roidb

    # Always return valid=True, since RPN minibatches are valid by design
    return True


def _get_rpn_blobs(im_height, im_width, foas, all_anchors, gt_boxes):
    total_anchors = all_anchors.shape[0]
    straddle_thresh = cfg.TRAIN.RPN_STRADDLE_THRESH

    if straddle_thresh >= 0:
        # Only keep anchors inside the image by a margin of straddle_thresh
        # Set TRAIN.RPN_STRADDLE_THRESH to -1 (or a large value) to keep all
        # anchors
        INF = 100000000
        inds_inside = np.where(
            (all_anchors[:, 0] >= -straddle_thresh) &
            (all_anchors[:, 1] >= -straddle_thresh) &
            (all_anchors[:, 2] < im_width + straddle_thresh) &
            (all_anchors[:, 3] < im_height + straddle_thresh)
        )[0]
        # keep only inside anchors
        anchors = all_anchors[inds_inside, :]
        foas_part = []

        for i in range(len(foas)):
            inds_inside_part = np.where(
                (foas[i][0][:,0] >= -straddle_thresh) &
                (foas[i][0][:,1] >= -straddle_thresh) &
                (foas[i][0][:,2] < im_width + straddle_thresh) &
                (foas[i][0][:,3] < im_height + straddle_thresh)
            )[0]
            foas_part.append(foas[i][0][inds_inside_part,:])
    else:
        inds_inside = np.arange(all_anchors.shape[0])
        anchors = all_anchors
    num_inside = len(inds_inside)

    logger.debug('total_anchors: %d', total_anchors)
    logger.debug('inds_inside: %d', num_inside)
    logger.debug('anchors.shape: %s', str(anchors.shape))

    # Compute anchor labels:
    # label=1 is positive, 0 is negative, -1 is don't care (ignore)
    labels = np.empty((num_inside, ), dtype=np.int32)
    labels.fill(-1)

    '''
    gt_bbox : x,y,x,y 
    anchor : x,y,x,y
    '''
    if len(gt_boxes) > 0:
        num_gt = gt_boxes.shape[0]
        # Compute overlaps between the anchors and the gt boxes overlaps
        anchor_by_gt_overlap = box_utils.bbox_overlaps(anchors, gt_boxes)
        iou_tensor = torch.from_numpy(anchor_by_gt_overlap).type(torch.float32)

        '''
        calculate the distance between bbox and gt_bbox based on center point
        '''
        gt_boxes_tensor = torch.from_numpy(gt_boxes).type(torch.float32)
        gt_cx = (gt_boxes_tensor[:, 2] + gt_boxes_tensor[:, 0]) / 2.0
        gt_cy = (gt_boxes_tensor[:, 3] + gt_boxes_tensor[:, 1]) / 2.0
        # coordinate of the center point of each gt
        gt_points = torch.stack((gt_cx, gt_cy), dim=1)

        anchors = torch.from_numpy(anchors).type(torch.float32)
        anchors_cx_per_im = (anchors[:, 2] + anchors[:, 0]) / 2.0
        anchors_cy_per_im = (anchors[:, 3] + anchors[:, 1]) / 2.0
        # coordinate of the center point of each anchor
        anchor_points = torch.stack((anchors_cx_per_im,anchors_cy_per_im),dim=1)

        # Calculate the distance between the coordinates of two center points
        distances = (anchor_points[:, None, :] - gt_points[None, :, :]).pow(2).sum(-1).sqrt()

        '''
        selecting candidates based on the center distance between anchor box and gt_bbox
        '''

        candidate_idxs = []
        star_idx = 0
        num_anchors_per_level = [len(anchors_per_level) for anchors_per_level in foas_part]
        num_anchors_per_loc = (len(cfg.RPN.SIZES) + 1) * len(cfg.RPN.ASPECT_RATIOS)
        ATSS_TOPK = 9
        for level, anchors_per_level in enumerate(foas_part):
            end_idx = star_idx + num_anchors_per_level[level]
            if anchors_per_level.shape[0] == 0:
                continue
            distances_per_level = distances[star_idx:end_idx, :]
            topk = min(ATSS_TOPK * num_anchors_per_loc, num_anchors_per_level[level])
            # Return the first k elements in the Tensor and the corresponding index value of the element
            _, topk_idxs_per_level = distances_per_level.topk(topk, dim=0, largest=False)
            candidate_idxs.append(topk_idxs_per_level + star_idx)
            star_idx = end_idx
        candidate_idxs = torch.cat(candidate_idxs, dim=0)

        '''
         Using the sum of mean and standard deviation as the IoU threshold to select final positive samples
        '''
        anchor_by_gt_overlap_tensor = iou_tensor
        candidate_ious = anchor_by_gt_overlap_tensor[candidate_idxs, torch.arange(num_gt).long()]
        iou_mean_per_gt = candidate_ious.mean(0)
        iou_std_per_gt = candidate_ious.std(0)
        iou_thresh_per_gt = iou_mean_per_gt + iou_std_per_gt
        is_pos_early = candidate_ious >= iou_thresh_per_gt[None, :]

        '''
        Limiting the final positive samples’ center to object
        '''

        anchor_num = anchors_cx_per_im.shape[0]
        for ng in range(num_gt):
            candidate_idxs[:, ng] += ng * anchor_num
        e_anchors_cx = anchors_cx_per_im.view(1, -1).expand(num_gt, anchor_num).contiguous().view(-1)
        e_anchors_cy = anchors_cy_per_im.view(1, -1).expand(num_gt, anchor_num).contiguous().view(-1)
        candidate_idxs = candidate_idxs.view(-1)
        l = e_anchors_cx[candidate_idxs].view(-1, num_gt) - gt_boxes_tensor[:, 0]
        t = e_anchors_cy[candidate_idxs].view(-1, num_gt) - gt_boxes_tensor[:, 1]
        r = gt_boxes_tensor[:, 2] - e_anchors_cx[candidate_idxs].view(-1, num_gt)
        b = gt_boxes_tensor[:, 3] - e_anchors_cy[candidate_idxs].view(-1, num_gt)
        is_in_gts = torch.stack([l, t, r, b], dim=1).min(dim=1)[0] > 0.01
        is_pos = is_pos_early & is_in_gts

        if torch.nonzero(is_pos).shape[0] == 0:
            is_pos = is_pos_early

        '''
        if an anchor box is assigned to multiple gts, the one with the highest IoU will be selected.
        '''

        ious_inf = torch.full_like(iou_tensor, -INF).t().contiguous().view(-1)
        index = candidate_idxs.view(-1)[is_pos.view(-1)]
        ious_inf[index] = iou_tensor.t().contiguous().view(-1)[index]
        ious_inf = ious_inf.view(num_gt, -1).t()

        anchor_to_gt_max, anchor_to_gt_argmax = ious_inf.max(dim=1)
        gt_to_anchor_max, gt_to_anchor_argmax = ious_inf.max(dim=0)
        # cls_labels_per_im = labels_per_im[anchors_to_gt_indexs]
        # cls_labels_per_im[anchors_to_gt_values == -INF] = 0
        # matched_gts = gt_boxes[anchors_to_gt_indexs]

        # Map from anchor to gt box that has highest overlap
        #anchor_to_gt_argmax = iou_tensor.argmax(axis=1)
        # For each anchor, amount of overlap with most overlapping gt box
        #anchor_to_gt_max = iou_tensor[np.arange(num_inside),
        #                                        anchor_to_gt_argmax]

        # Map from gt box to an anchor that has highest overlap
        #gt_to_anchor_argmax = iou_tensor.argmax(axis=0)
        # For each gt box, amount of overlap with most overlapping anchor
        # gt_to_anchor_max = iou_tensor[
        #     gt_to_anchor_argmax,
        #     np.arange(iou_tensor.shape[1])
        # ]
        # Find all anchors that share the max overlap amount
        # (this includes many ties)
        gt_to_anchor_max_np = gt_to_anchor_max.cpu().numpy()
        anchor_to_gt_max_np = anchor_to_gt_max.cpu().numpy()
        # anchor_to_gt_max_np.dtype = 'int64'
        anchors_with_max_overlap = np.where(
            anchor_by_gt_overlap == gt_to_anchor_max_np
        )[0]

        # Fg label: for each gt use anchors with highest overlap
        # (including ties)
        labels[anchors_with_max_overlap] = 1
        # Fg label: above threshold IOU
        labels[anchor_to_gt_max_np >= cfg.TRAIN.RPN_POSITIVE_OVERLAP] = 1

    # subsample positive labels if we have too many
    num_fg = int(cfg.TRAIN.RPN_FG_FRACTION * cfg.TRAIN.RPN_BATCH_SIZE_PER_IM)
    fg_inds = np.where(labels == 1)[0]
    if len(fg_inds) > num_fg:
        disable_inds = npr.choice(
            fg_inds, size=(len(fg_inds) - num_fg), replace=False
        )
        labels[disable_inds] = -1
    fg_inds = np.where(labels == 1)[0]

    # subsample negative labels if we have too many
    # (samples with replacement, but since the set of bg inds is large most
    # samples will not have repeats)
    num_bg = cfg.TRAIN.RPN_BATCH_SIZE_PER_IM - np.sum(labels == 1)
    bg_inds = np.where(anchor_to_gt_max_np < cfg.TRAIN.RPN_NEGATIVE_OVERLAP)[0]
    if len(bg_inds) > num_bg:
        enable_inds = bg_inds[npr.randint(len(bg_inds), size=num_bg)]
        labels[enable_inds] = 0
    bg_inds = np.where(labels == 0)[0]

    bbox_targets = np.zeros((num_inside, 4), dtype=np.float32)
    # try:
    #     anchor_to_gt_max_np.dtype = 'int32'
    # except:
    #     print(anchor_to_gt_max_np.dtype)
    anchor_to_gt_max_np.dtype = 'int32'
    bbox_targets[fg_inds, :] = data_utils.compute_targets(
        anchors[fg_inds, :], gt_boxes[anchor_to_gt_argmax[fg_inds], :]
    )

    # Bbox regression loss has the form:
    #   loss(x) = weight_outside * L(weight_inside * x)
    # Inside weights allow us to set zero loss on an element-wise basis
    # Bbox regression is only trained on positive examples so we set their
    # weights to 1.0 (or otherwise if config is different) and 0 otherwise
    bbox_inside_weights = np.zeros((num_inside, 4), dtype=np.float32)
    bbox_inside_weights[labels == 1, :] = (1.0, 1.0, 1.0, 1.0)

    # The bbox regression loss only averages by the number of images in the
    # mini-batch, whereas we need to average by the total number of example
    # anchors selected
    # Outside weights are used to scale each element-wise loss so the final
    # average over the mini-batch is correct
    bbox_outside_weights = np.zeros((num_inside, 4), dtype=np.float32)
    # uniform weighting of examples (given non-uniform sampling)
    num_examples = np.sum(labels >= 0)
    bbox_outside_weights[labels == 1, :] = 1.0 / num_examples
    bbox_outside_weights[labels == 0, :] = 1.0 / num_examples

    # Map up to original set of anchors
    labels = data_utils.unmap(labels, total_anchors, inds_inside, fill=-1)
    bbox_targets = data_utils.unmap(
        bbox_targets, total_anchors, inds_inside, fill=0
    )
    bbox_inside_weights = data_utils.unmap(
        bbox_inside_weights, total_anchors, inds_inside, fill=0
    )
    bbox_outside_weights = data_utils.unmap(
        bbox_outside_weights, total_anchors, inds_inside, fill=0
    )

    # Split the generated labels, etc. into labels per each field of anchors
    blobs_out = []
    start_idx = 0
    for foa in foas:
        H = foa.field_size
        W = foa.field_size
        A = foa.num_cell_anchors
        end_idx = start_idx + H * W * A
        _labels = labels[start_idx:end_idx]
        _bbox_targets = bbox_targets[start_idx:end_idx, :]
        _bbox_inside_weights = bbox_inside_weights[start_idx:end_idx, :]
        _bbox_outside_weights = bbox_outside_weights[start_idx:end_idx, :]
        start_idx = end_idx

        # labels output with shape (1, A, height, width)
        _labels = _labels.reshape((1, H, W, A)).transpose(0, 3, 1, 2)
        # bbox_targets output with shape (1, 4 * A, height, width)
        _bbox_targets = _bbox_targets.reshape(
            (1, H, W, A * 4)).transpose(0, 3, 1, 2)
        # bbox_inside_weights output with shape (1, 4 * A, height, width)
        _bbox_inside_weights = _bbox_inside_weights.reshape(
            (1, H, W, A * 4)).transpose(0, 3, 1, 2)
        # bbox_outside_weights output with shape (1, 4 * A, height, width)
        _bbox_outside_weights = _bbox_outside_weights.reshape(
            (1, H, W, A * 4)).transpose(0, 3, 1, 2)
        blobs_out.append(
            dict(
                rpn_labels_int32_wide=_labels,
                rpn_bbox_targets_wide=_bbox_targets,
                rpn_bbox_inside_weights_wide=_bbox_inside_weights,
                rpn_bbox_outside_weights_wide=_bbox_outside_weights
            )
        )
    return blobs_out[0] if len(blobs_out) == 1 else blobs_out
