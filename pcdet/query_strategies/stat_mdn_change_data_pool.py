import glob
import os
import tqdm
import numpy as np
import pandas as pd
import torch
import random

from pcdet.datasets import build_al_dataloader
from pcdet.models import load_data_to_gpu, build_network, model_fn_decorator

from pcdet.utils.al_utils import calculate_category_entropy
import matplotlib.pyplot as plt


def stat_mdn_strategies(cfg, args, model, ckpt_dir, logger, slice_set, search_num_each, rules=1, score_thresh=0.3,
                        eu_theta=1, au_theta=1, score_plus=False, score_reverse=False, k1=3, stat_k=None):
    """


    :param eu_theta:
    :param cfg:
    :param args:
    :param model:
    :param ckpt_dir:
    :param logger:
    :param slice_set:
    :param search_num_each: 每一轮主动学习搜索的数量
    :param rules: int, 选择策略
    :param score_thresh: 对预测结果根据阈值过滤，一些低置信度的结果会对最后的选择产生影响，默认是0.3
    :param eu_theta:
    :param au_theta: 当使用au+eu策略时，au的权重
    :param score_plus: 在使用au策略时，是否使用置信度加权，即 score*au
    :param score_reverse: 在使用au策略时，是否使用1-置信度加权，即 (1-score)*au
    :param consider_other: 选择的时候是否考虑其他类，默认不考虑
    :param k1: 如果使用两阶段策略（先根据类别熵进行第一阶段选择），第一阶段的选择数量相较于search_num_each的比例
    :param stat_k: dict， {"cate_k": float, "scale_k": float, "rot_k": float, ...}
                 考虑类别熵 大小熵 角度熵...的时候，相应的权重dict
    :return:
    """
    (label_pool, unlabel_pool) = slice_set

    _, _, unlabel_dataset, unlabel_dataloader = build_al_dataloader(
        dataset_cfg=cfg.DATA_CONFIG,
        class_names=cfg.CLASS_NAMES,
        batch_size=args.batch_size,
        slice_set=slice_set,
        seed=666 if args.fix_random_seed else None,
        workers=args.workers
    )

    logger.info('**********************Start search:label pool:%s  unlabel_pool:%s**********************' %
                (len(label_pool), len(unlabel_pool)))

    model.load_params_from_file(filename=ckpt_dir, logger=logger)
    model.cuda()
    model.eval()

    progress_bar = tqdm.tqdm(total=len(unlabel_dataloader), leave=True, desc='search', dynamic_ncols=True)

    det_annos = []
    for i, batch_dict in enumerate(unlabel_dataloader):
        load_data_to_gpu(batch_dict)
        with torch.no_grad():
            pred_dicts, ret_dict = model(batch_dict)

            annos = unlabel_dataloader.dataset.generate_al_prediction_dicts(
                batch_dict, pred_dicts, unlabel_dataset.class_names
            )
        det_annos += annos
        if cfg.LOCAL_RANK == 0:
            progress_bar.update()

    au_list = []
    eu_list = []
    score_list = []
    id_list = []
    car_num_list = []
    cate_entropy_list = []  # the category entropy
    scale_entropy_list = []
    rot_entropy_list = []
    empty_list = []  # the empty frame

    # the type we consider

    for idx, det in enumerate(det_annos):
        if len(det['name'].tolist()) == 0:
            empty_list.append(det['frame_id'])
        else:
            choose_slice = det['score'] >= score_thresh
            if np.sum(choose_slice) == 0:  # means threre is no object (with score >= thresh) in this frame
                empty_list.append(det['frame_id'])
                continue

            # cal the category entropy
            cate_entropy = calculate_category_entropy(det['name'][choose_slice])
            cate_entropy_list.append(cate_entropy)

            # cal the scale entropy
            boxes_lidar_choose = det['boxes_lidar'][choose_slice]
            scale_l = np.array(
                [boxes_lidar_choose[i, 3] * boxes_lidar_choose[i, 4] for i in range(len(boxes_lidar_choose))])
            scale_entropy_list.append(cal_scale_entropy(scale_l, s_min=0, s_max=40, interval=5))

            # cal the rot entropy
            rot_l = det['rotation_y'][choose_slice]
            rot_entropy_list.append(cal_rotation_entropy(rot_l, interval=60))

            if det['al'].ndim == 1:  # if we use corner loss varance, the shape of al is (N,)
                au_list.append(det['al'][choose_slice])
            else:
                au_list.append(np.max(det['al'][choose_slice], axis=1))

            score_list.append(det['score'][choose_slice])
            car_num_list.append(len(choose_slice))

            if rules in [7, 8, 9, 12, 13]:  # use eu or eu + au
                # if we use mdn than the eu is calulated by each box, so it has the same length of au, and the type is ndarray
                if isinstance(det['ep'], np.ndarray):
                    eu_list.append(det['ep'][choose_slice])
                # if use mc dropout, we only get one value for each frame
                else:
                    eu_list.append(float(det['ep']))
            elif rules == 6:
                eu_list.append(float(det['ep_mc']))

            id_list.append(det['frame_id'])

    id_list = np.array(id_list)

    # use 0-1 normalize on au data
    max_al_uc = np.max([val for sub in au_list for val in sub])
    min_al_uc = np.min([val for sub in au_list for val in sub])
    # some trick
    if score_reverse:
        for i in range(len(score_list)):
            score_list[i] = 1 - score_list[i]
    if score_plus:
        for i in range(len(au_list)):
            # au_list[i] = ((au_list[i] - mean_al_uc) / stdev_al_uc) * score_list[i]
            au_list[i] = ((au_list[i] - min_al_uc) / (max_al_uc - min_al_uc)) * score_list[i]
    else:
        for i in range(len(au_list)):
            # au_list[i] = (au_list[i] - mean_al_uc) / stdev_al_uc
            au_list[i] = (au_list[i] - min_al_uc) / (max_al_uc - min_al_uc)

    if rules == 11:
        au_list = score_list

    au_list = combine_uncer_in_frame(au_list, rules=rules)

    # use 0-1 on eu data if we use eu or eu + au
    if len(eu_list) > 0 and isinstance(eu_list[0], float):
        eu_list = np.array(eu_list)
        eu_list = (eu_list - np.min(eu_list)) / (np.max(eu_list) - np.min(eu_list))
        # we need to use 0-1 normalize again on au_list because we use eu + au
        au_list = (au_list - np.min(au_list)) / (np.max(au_list) - np.min(au_list))

    if len(eu_list) > 0 and isinstance(eu_list[0], np.ndarray):
        max_ep_uc = np.max([val for sub in eu_list for val in sub])
        min_ep_uc = np.min([val for sub in eu_list for val in sub])
        for i in range(len(eu_list)):
            eu_list[i] = ((eu_list[i] - min_ep_uc) / (max_ep_uc - min_ep_uc))
        eu_list = combine_uncer_in_frame(eu_list, rules=rules)
        # use 0-1 again on eu and au
        au_list = (au_list - np.min(au_list)) / (np.max(au_list) - np.min(au_list))
        eu_list = (eu_list - np.min(eu_list)) / (np.max(eu_list) - np.min(eu_list))

    # next to choose the score_list (au or eu or eu + au)
    if rules in [7, 8, 9, 12, 13]:
        # choose_score_list = eu_list + eu_theta * au_list  # 这里有问题eu的比例放错位置了
        choose_score_list = eu_theta * eu_list + au_theta * au_list
    elif rules == 6:
        choose_score_list = eu_list
    else:
        choose_score_list = au_list

    # 如果使用二阶段策略，并且一阶段使用类别熵
    if rules == 9:
        cate_entropy_list = np.array(cate_entropy_list)
        sorted_indices_k1 = np.argsort(cate_entropy_list)[::-1][:int(k1 * search_num_each)]

        choose_score_list = choose_score_list[sorted_indices_k1]
        id_list = id_list[sorted_indices_k1]

    # 如果使用二阶段策略，并且一阶段使用统计复杂度
    if rules == 12:
        cate_entropy_list = np.array(cate_entropy_list)
        scale_entropy_list = np.array(scale_entropy_list)
        rot_entropy_list = np.array(rot_entropy_list)

        scale_entropy_list = (scale_entropy_list - np.min(scale_entropy_list)) / (
                    np.max(scale_entropy_list) - np.min(scale_entropy_list))
        rot_entropy_list = (rot_entropy_list - np.min(rot_entropy_list)) / (
                    np.max(rot_entropy_list) - np.min(rot_entropy_list))
        # cate_entropy_list = (cate_entropy_list - np.min(cate_entropy_list)) / (np.max(cate_entropy_list) - np.min(cate_entropy_list))

        combine_entropy_list = stat_k["cate_k"] * cate_entropy_list + stat_k["scale_k"] * scale_entropy_list + stat_k[
            "rot_k"] * rot_entropy_list
        sorted_indices_k1 = np.argsort(combine_entropy_list)[::-1][:int(k1 * search_num_each)]

        choose_score_list = choose_score_list[sorted_indices_k1]
        id_list = id_list[sorted_indices_k1]

    # 直接使用类别熵作为筛选依据
    if rules == 10:
        choose_score_list = np.array(cate_entropy_list)

    if rules == 4:
        choose_score_list = np.array(car_num_list)

    sorted_indices = np.argsort(choose_score_list)[::-1]
    # this sort list not include the empty frame
    sort_id_list = id_list[sorted_indices]
    # next we need to add the empty frame
    # sort_id_list = np.concatenate((sort_id_list, np.array(empty_list)), axis=0)

    choose_id = sort_id_list[:search_num_each]

    # create the dict map sample id to index in dataset pool
    id_to_idx = {unlabel_dataset.kitti_infos[i]['image']['image_idx']: i for i in
                 range(len(unlabel_dataset.kitti_infos))}

    choose_idx = [id_to_idx[i] for i in choose_id]

    label_pool = list(set(label_pool) | (set(choose_idx)))
    unlabel_pool = list(set(unlabel_pool) - set(choose_idx))
    random.shuffle(label_pool)
    random.shuffle(unlabel_pool)

    if hasattr(unlabel_dataset, 'use_shared_memory') and unlabel_dataset.use_shared_memory:
        unlabel_dataset.clean_shared_memory()

    logger.info('\n**********************End search:label pool:%s  unlabel_pool:%s**********************' %
                (len(label_pool), len(unlabel_pool)))

    return label_pool, unlabel_pool, choose_id


def combine_uncer_in_frame(uncertain_list, rules):
    """

    :param uncertain_list: list [ndarray]
    :param rules:
    :return:
    """
    for i in range(len(uncertain_list)):
        if rules == 1:
            uncertain_list[i] = np.max(uncertain_list[i])
        elif rules == 2 or rules == 8:
            uncertain_list[i] = np.sum(uncertain_list[i])
        else:
            # if the rule is 6 or 7, we use eu or eu + au, so we need to use mean the express the au of frame
            uncertain_list[i] = np.mean(uncertain_list[i])

    return np.array(uncertain_list)


def cal_scale_entropy(scale_data, s_min=0, s_max=40, interval=5):
    scale_data = np.clip(scale_data, s_min, s_max)
    s_slice = range(s_min, s_max + 1, interval)
    c = pd.cut(scale_data, s_slice)
    sum_num = np.sum(c.value_counts().values)
    frequency = [i / sum_num for i in c.value_counts().values]
    en = entropy(frequency)

    return en


def cal_rotation_entropy(r_list, interval=15):
    """

    :param r_list: ndarray, rotation array (弧度)
    :param interval:
    :return:
    """

    r_list = r_list * 180 / np.pi
    r_list = r_list - np.floor(r_list / 360.0) * 360

    r_list = np.clip(r_list, 0.01, 360)

    r_slice = range(0, 361, interval)
    c = pd.cut(r_list, r_slice)
    sum_num = np.sum(c.value_counts().values)
    frequency = [i / sum_num for i in c.value_counts().values]
    en = entropy(frequency)
    return en


def entropy(frequency):
    fre = np.array(frequency)
    fre = fre[fre != 0]
    en = np.sum(-fre * np.log2(fre))
    return en
