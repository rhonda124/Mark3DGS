#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import torch
import torch.nn.functional as F
import numpy as np
from utils.general_utils import inverse_sigmoid, get_expon_lr_func, build_rotation
from torch import nn
import os
from utils.system_utils import mkdir_p
from plyfile import PlyData, PlyElement
from utils.sh_utils import RGB2SH
from simple_knn._C import distCUDA2
from utils.graphics_utils import BasicPointCloud
from utils.general_utils import strip_symmetric, build_scaling_rotation
from utils.general_utils import splitBy3, mortonEncode

from vector_quantize_pytorch import VectorQuantize, ResidualVQ
import tinycudann as tcnn

from dahuffman import HuffmanCodec
from dahuffman.huffmancodec import PrefixCodec
import math
from einops import reduce

import logging

class GaussianModel:

    def setup_functions(self):
        def build_covariance_from_scaling_rotation(scaling, scaling_modifier, rotation):
            L = build_scaling_rotation(scaling_modifier * scaling, rotation)
            actual_covariance = L @ L.transpose(1, 2)
            symm = strip_symmetric(actual_covariance)
            return symm
        
        self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log

        self.covariance_activation = build_covariance_from_scaling_rotation

        self.opacity_activation = torch.sigmoid
        self.inverse_opacity_activation = inverse_sigmoid

        self.rotation_activation = torch.nn.functional.normalize


    def __init__(self, model):
        self.watermarked_points = []
        self.watermark_value = "11225917"
        # self.watermark_value = "1238912912259179123891239872918012389129110291791220932398729191"
        # self.watermark_value = "91291225917912389129122591791238"
        self.active_sh_degree = 0
        self.max_sh_degree = 0
        self._xyz = torch.empty(0)
        self._scaling = torch.empty(0)
        self._rotation = torch.empty(0)
        self._opacity = torch.empty(0)
        self._mask = torch.empty(0)
        self.max_radii2D = torch.empty(0)
        self.xyz_gradient_accum = torch.empty(0)
        self.denom = torch.empty(0)
        self.optimizer = None
        self.percent_dense = 0
        self.spatial_lr_scale = 0
        self.setup_functions()
        
        self.vq_scale = ResidualVQ(dim = 3, codebook_size = model.rvq_size, num_quantizers = model.rvq_num, commitment_weight = 0., kmeans_init = True, kmeans_iters = 1, ema_update = False, learnable_codebook=True, in_place_codebook_optimizer=lambda *args, **kwargs: torch.optim.Adam(*args, **kwargs, lr=0.0001)).cuda()
        self.vq_rot = ResidualVQ(dim = 4, codebook_size = model.rvq_size, num_quantizers = model.rvq_num, commitment_weight = 0., kmeans_init = True, kmeans_iters = 1, ema_update = False, learnable_codebook=True, in_place_codebook_optimizer=lambda *args, **kwargs: torch.optim.Adam(*args, **kwargs, lr=0.0001)).cuda()
        self.rvq_bit = math.log2(model.rvq_size)
        self.rvq_num = model.rvq_num
        self.recolor = tcnn.Encoding(
                 n_input_dims=3,
                 encoding_config={
                    "otype": "HashGrid",
                    "n_levels": 16,
                    "n_features_per_level": 2,
                    "log2_hashmap_size": model.max_hashmap,
                    "base_resolution": 16,
                    "per_level_scale": 1.447,
                },
        )
        self.watermark_scales = [1.5, 1.382, 1.285, 1.184, 1.123, 1.056, 1.728, 1.618]
        self.max_hashmap = model.max_hashmap
        # self.watermark_recolor = tcnn.Encoding(
        #          n_input_dims=3,
        #          encoding_config={
        #             "otype": "HashGrid",
        #             "n_levels": 16,
        #             "n_features_per_level": 2,
        #             "log2_hashmap_size": model.max_hashmap,
        #             "base_resolution": 16,
        #             "per_level_scale": self.watermark_scale if self.watermark_scale else 1.5,
        #         },
        # )
        self.direction_encoding = tcnn.Encoding(
            n_input_dims=3,
            encoding_config={
                "otype": "SphericalHarmonics",
                "degree": 3 
            },
            )
        self.mlp_head = tcnn.Network(
                n_input_dims=(self.direction_encoding.n_output_dims+self.recolor.n_output_dims),
                n_output_dims=3,
                network_config={
                    "otype": "FullyFusedMLP",
                    "activation": "ReLU",
                    "output_activation": "None",
                    "n_neurons": 64,
                    "n_hidden_layers": 2,
                },
            )

    def extract_watermark(self): # for testing
        if not self.watermarked_points:
            return torch.tensor([], device="cuda"), torch.tensor([], device="cuda")
        
        watermark_opacity = self.watermarked_points['group_0']['quantized'][0].to(torch.float32).item()
        print(f"watermark_opacity after sigmoid: {watermark_opacity}")

        watermarked_points = torch.isclose(self._opacity, torch.tensor(watermark_opacity, device="cuda"), rtol=1e-7, atol=1e-8)
        opacity_watermarked_indices = torch.where(watermarked_points)[0]

        xyz = self.contract_to_unisphere(self.get_xyz[opacity_watermarked_indices].half(), 
                                    torch.tensor([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0], device="cuda"))
        watermark_features = self.watermark_recolor(xyz).detach().to(torch.float32)
        stored_features = self._feature[opacity_watermarked_indices]
        feature_matches = torch.all(torch.isclose(watermark_features, stored_features, rtol=1e-7, atol=1e-8), dim=1)
        
        watermarked_indices = opacity_watermarked_indices[feature_matches]
        watermarked_positions = self._xyz[watermarked_indices].clone().half().float()
        print(f"Found {len(watermarked_indices)} watermarked points after filtering, {len(opacity_watermarked_indices)} opacity matched points")
        
        return watermarked_indices, watermarked_positions

    def extract_opacity(self):
        # 计算所有opacity匹配点的特征
        xyz = self.contract_to_unisphere(self.get_xyz.half(), 
                                    torch.tensor([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0], device="cuda"))
        watermarked_opacities = []
        for watermark_scale in self.watermark_scales:
            self.watermark_recolor = tcnn.Encoding(
                n_input_dims=3,
                encoding_config={
                    "otype": "HashGrid",
                    "n_levels": 16,
                    "n_features_per_level": 2,
                    "log2_hashmap_size": self.max_hashmap,
                    "base_resolution": 16,
                    "per_level_scale": watermark_scale,
                    },
            )
            watermark_features = self.watermark_recolor(xyz).detach().to(torch.float32)
            stored_features = self._feature
            feature_matches = torch.all(torch.isclose(watermark_features, stored_features, rtol=1e-7, atol=1e-8), dim=1)
            watermarked_indices = torch.where(feature_matches)[0]
            watermarked_opacity = self._opacity[watermarked_indices]
            watermarked_opacities.append((watermark_scale, watermarked_opacity))

        return watermarked_opacities 

    def compare_watermarks(self): # for testing
        current_indices, current_watermarked_positions = self.extract_watermark()
        current_watermarked_list = current_indices.cpu().tolist()
        
        if self.watermarked_points:
            all_recorded_indices = []
            for group_key in self.watermarked_points:
                all_recorded_indices.extend(self.watermarked_points[group_key]['indices'])
            
            same_indices = []
            different_indices = []
            same_opacities = [] 
            
            for idx in current_watermarked_list:
                if idx in all_recorded_indices:
                    same_indices.append(idx)
                    opacity_value = self._opacity[idx].to(torch.float32)
                    same_opacities.append(float(opacity_value))
                else:
                    different_indices.append(idx)
                    
            lost_indices = [idx for idx in all_recorded_indices if idx not in current_watermarked_list]
            
            return {
                "same": len(same_indices),
                "different": len(different_indices),
                "lost": len(lost_indices),
                "total_recorded": len(all_recorded_indices),
                "total_current": len(current_watermarked_list),
                "all_recorded": all_recorded_indices,
                "all_current": current_indices,
                "same_indices": same_indices,
                "same_opacities": ["{:.32f}".format(opacity) for opacity in same_opacities],
                # "same_scaling": self._scaling[same_indices]
            }
        else:
            return {
                "same": 0,
                "different": len(current_watermarked_list),
                "lost": 0,
                "total_recorded": 0,
                "total_current": len(current_watermarked_list),
                "all_recorded": [],
                "all_current": current_indices,
                "same_indices": [],
                "same_opacities": [],
                # "same_scaling": []
            }

    def capture(self):
        return (
            self.active_sh_degree,
            self._xyz,
            self._scaling,
            self._rotation,
            self._opacity,
            self.max_radii2D,
            self.xyz_gradient_accum,
            self.denom,
            self.optimizer.state_dict(),
            self.spatial_lr_scale,
        )
    
    def restore(self, model_args, training_args):
        (self.active_sh_degree, 
        self._xyz,
        self._scaling, 
        self._rotation, 
        self._opacity,
        self.max_radii2D, 
        xyz_gradient_accum, 
        denom,
        opt_dict, 
        self.spatial_lr_scale) = model_args
        self.training_setup(training_args)
        self.xyz_gradient_accum = xyz_gradient_accum
        self.denom = denom
        self.optimizer.load_state_dict(opt_dict)

    @property
    def get_scaling(self):
        return self.scaling_activation(self._scaling)
    
    @property
    def get_rotation(self):
        return self.rotation_activation(self._rotation)
    
    @property
    def get_xyz(self):
        return self._xyz
    
    @property
    def get_opacity(self):
        return self.opacity_activation(self._opacity)
    
    def get_covariance(self, scaling_modifier = 1):
        return self.covariance_activation(self.get_scaling, scaling_modifier, self._rotation)

    def oneupSHdegree(self):
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1

    def create_from_pcd(self, pcd : BasicPointCloud, spatial_lr_scale : float):
        self.spatial_lr_scale = spatial_lr_scale
        fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
        fused_color = RGB2SH(torch.tensor(np.asarray(pcd.colors)).float().cuda())
        features = torch.zeros((fused_color.shape[0], 3, (self.max_sh_degree + 1) ** 2)).float().cuda()
        features[:, :3, 0 ] = fused_color
        features[:, 3:, 1:] = 0.0

        print("Number of points at initialisation : ", fused_point_cloud.shape[0])

        dist2 = torch.clamp_min(distCUDA2(torch.from_numpy(np.asarray(pcd.points)).float().cuda()), 0.0000001)
        scales = torch.log(torch.sqrt(dist2))[...,None].repeat(1, 3)
        rots = torch.zeros((fused_point_cloud.shape[0], 4), device="cuda")
        rots[:, 0] = 1

        opacities = inverse_sigmoid(0.1 * torch.ones((fused_point_cloud.shape[0], 1), dtype=torch.float, device="cuda"))

        self._xyz = nn.Parameter(fused_point_cloud.requires_grad_(True))
        self._scaling = nn.Parameter(scales.requires_grad_(True))
        self._rotation = nn.Parameter(rots.requires_grad_(True))
        self._opacity = nn.Parameter(opacities.requires_grad_(True))
        self._mask = nn.Parameter(torch.ones((fused_point_cloud.shape[0], 1), device="cuda").requires_grad_(True))
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def training_setup(self, training_args):
        self.percent_dense = training_args.percent_dense
        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")

        other_params = []
        for params in self.recolor.parameters():
            other_params.append(params)
        for params in self.mlp_head.parameters():
            other_params.append(params)
            
        l = [
            {'params': [self._xyz], 'lr': training_args.position_lr_init * self.spatial_lr_scale, "name": "xyz"},
            {'params': [self._opacity], 'lr': training_args.opacity_lr, "name": "opacity"},
            {'params': [self._scaling], 'lr': training_args.scaling_lr, "name": "scaling"},
            {'params': [self._rotation], 'lr': training_args.rotation_lr, "name": "rotation"},
            {'params': [self._mask], 'lr': training_args.mask_lr, "name": "mask"}
        ]

        self.optimizer = torch.optim.Adam(l, lr=0.0, eps=1e-15)
        self.optimizer_net = torch.optim.Adam(other_params, lr=training_args.net_lr, eps=1e-15)
        self.scheduler_net = torch.optim.lr_scheduler.ChainedScheduler(
        [
            torch.optim.lr_scheduler.LinearLR(
            self.optimizer_net, start_factor=0.01, total_iters=100
        ),
            torch.optim.lr_scheduler.MultiStepLR(
            self.optimizer_net,
            milestones=training_args.net_lr_step,
            gamma=0.33,
        ),
        ]
        )
        self.xyz_scheduler_args = get_expon_lr_func(lr_init=training_args.position_lr_init*self.spatial_lr_scale,
                                                    lr_final=training_args.position_lr_final*self.spatial_lr_scale,
                                                    lr_delay_mult=training_args.position_lr_delay_mult,
                                                    max_steps=training_args.position_lr_max_steps)

    def update_learning_rate(self, iteration):
        ''' Learning rate scheduling per step '''
        for param_group in self.optimizer.param_groups:
            if param_group["name"] == "xyz":
                lr = self.xyz_scheduler_args(iteration)
                param_group['lr'] = lr
                return lr

    def construct_list_of_attributes(self):
        l = ['x', 'y', 'z', 'nx', 'ny', 'nz']
        l.append('opacity')
        for i in range(self._scaling.shape[1]):
            l.append('scale_{}'.format(i))
        for i in range(self._rotation.shape[1]):
            l.append('rot_{}'.format(i))
        return l

    def save_ply(self, path):
        mkdir_p(os.path.dirname(path))

        xyz = self._xyz.detach().cpu().numpy()
        normals = np.zeros_like(xyz)
        opacities = self._opacity.detach().cpu().numpy()
        scale = self._scaling.detach().cpu().numpy()
        rotation = self._rotation.detach().cpu().numpy()
        features = self._feature.detach().cpu().numpy()

        # 创建扩展的属性列表，包含特征
        dtype_full = [(attribute, 'f4') for attribute in self.construct_list_of_attributes()]
        # 为每个特征维度添加属性
        for i in range(features.shape[1]):
            dtype_full.append((f'feature_{i}', 'f4'))

        elements = np.empty(xyz.shape[0], dtype=dtype_full)
        # 合并所有属性，包括特征
        attributes = np.concatenate((xyz, normals, opacities, scale, rotation, features), axis=1)
        elements[:] = list(map(tuple, attributes))
        el = PlyElement.describe(elements, 'vertex')
        PlyData([el]).write(path)

    def save_npz(self, path):
        mkdir_p(os.path.dirname(path))

        save_dict = dict()
        
        save_dict["xyz"] = self._xyz.detach().cpu().half().numpy()
        save_dict["opacity"] = self._opacity.detach().cpu().half().numpy()
        save_dict["scale"] = np.packbits(np.unpackbits(self.sca_idx.unsqueeze(-1).cpu().numpy().astype(np.uint8), axis=-1, count=int(self.rvq_bit), bitorder='little').flatten(), axis=None)
        save_dict["rotation"] = np.packbits(np.unpackbits(self.rot_idx.unsqueeze(-1).cpu().numpy().astype(np.uint8), axis=-1, count=int(self.rvq_bit), bitorder='little').flatten(), axis=None)
        save_dict["hash"] = self.recolor.params.cpu().half().numpy()
        save_dict["mlp"] = self.mlp_head.params.cpu().half().numpy()
        save_dict["codebook_scale"] = self.vq_scale.cpu().state_dict()
        save_dict["codebook_rotation"] = self.vq_rot.cpu().state_dict()
        save_dict["rvq_info"] = np.array([int(self.rvq_num), int(self.rvq_bit)])
        
        np.savez(path, **save_dict)
        
    def save_npz_pp(self, path):
        mkdir_p(os.path.dirname(path))

        save_dict = dict()
        
        save_dict["xyz"] = self._xyz.detach().cpu().half().numpy()
        save_dict["opacity"] = np.frombuffer(self.huf_opa, dtype=np.uint8)
        save_dict["scale"] = np.frombuffer(self.huf_sca, dtype=np.uint8)
        save_dict["rotation"] = np.frombuffer(self.huf_rot, dtype=np.uint8)
        save_dict["hash"] = np.frombuffer(self.huf_hash, dtype=np.uint8)
        save_dict["mlp"] = self.mlp_head.params.cpu().half().numpy()
        save_dict["huftable_opacity"] = self.tab_opa
        save_dict["huftable_scale"] = self.tab_sca
        save_dict["huftable_rotation"] = self.tab_rot
        save_dict["huftable_hash"] = self.tab_hash
        save_dict["codebook_scale"] = self.vq_scale.cpu().state_dict()
        save_dict["codebook_rotation"] = self.vq_rot.cpu().state_dict()
        save_dict["minmax_opacity"] = self.minmax_opa.numpy()
        save_dict["minmax_hash"] = self.minmax_hash.numpy()
        save_dict["rvq_info"] = np.array([int(self.rvq_num), int(self.rvq_bit)])
        
        np.savez_compressed(path+"_pp", **save_dict)

    def reset_opacity(self):
        opacities_new = inverse_sigmoid(torch.min(self.get_opacity, torch.ones_like(self.get_opacity)*0.01))
        optimizable_tensors = self.replace_tensor_to_optimizer(opacities_new, "opacity")
        self._opacity = optimizable_tensors["opacity"]

    def load_model(self, path):
        if os.path.isfile(path + '_pp.npz'):
            path = path + '_pp.npz'
            print("Loading ", path)
            load_dict = np.load(path, allow_pickle=True)

            codec = PrefixCodec(load_dict["huftable_opacity"].item())
            opacity = torch.tensor(codec.decode(load_dict["opacity"]))

            codec = PrefixCodec(load_dict["huftable_scale"].item())
            scale = codec.decode(load_dict["scale"])

            codec = PrefixCodec(load_dict["huftable_rotation"].item())
            rotation = codec.decode(load_dict["rotation"])

            codec = PrefixCodec(load_dict["huftable_hash"].item())
            hashgrid = torch.tensor(codec.decode(load_dict["hash"]))

            opacity = (float(load_dict["minmax_opacity"][1]) - float(load_dict["minmax_opacity"][0]))*opacity/255.0 + float(load_dict["minmax_opacity"][0])
            hashgrid = (float(load_dict["minmax_hash"][1]) - float(load_dict["minmax_hash"][0]))*hashgrid/255.0 + float(load_dict["minmax_hash"][0])

            self.vq_scale.load_state_dict(load_dict["codebook_scale"].item())
            self.vq_rot.load_state_dict(load_dict["codebook_rotation"].item())
            scale_codes = self.vq_scale.get_codes_from_indices(torch.tensor(scale).cuda().reshape(-1,1,load_dict["rvq_info"][0]))
            scale = self.vq_scale.project_out(reduce(scale_codes, 'q ... -> ...', 'sum'))
            rotation_codes = self.vq_rot.get_codes_from_indices(torch.tensor(rotation).cuda().reshape(-1,1,load_dict["rvq_info"][0]))
            rotation = self.vq_rot.project_out(reduce(rotation_codes, 'q ... -> ...', 'sum'))

            self._xyz = nn.Parameter(torch.from_numpy(load_dict["xyz"]).cuda().float().requires_grad_(True))
            self._opacity = nn.Parameter(opacity.cuda().reshape(-1,1).float().requires_grad_(True))
            self._scaling = nn.Parameter(scale.squeeze(1).requires_grad_(True))
            self._rotation = nn.Parameter(rotation.squeeze(1).requires_grad_(True))
            self.recolor.params = nn.Parameter(hashgrid.cuda().half().requires_grad_(True))
            self.mlp_head.params = nn.Parameter(torch.from_numpy(load_dict["mlp"]).cuda().half().requires_grad_(True))
        elif os.path.isfile(path + '.npz'):
            path = path + '.npz'
            print("Loading ", path)
            load_dict = np.load(path, allow_pickle=True)

            scale = np.packbits(np.unpackbits(load_dict["scale"], axis=None)[:load_dict["xyz"].shape[0]*load_dict["rvq_info"][0]*load_dict["rvq_info"][1]].reshape(-1, load_dict["rvq_info"][1]), axis=-1, bitorder='little')
            rotation = np.packbits(np.unpackbits(load_dict["rotation"], axis=None)[:load_dict["xyz"].shape[0]*load_dict["rvq_info"][0]*load_dict["rvq_info"][1]].reshape(-1, load_dict["rvq_info"][1]), axis=-1, bitorder='little')

            self.vq_scale.load_state_dict(load_dict["codebook_scale"].item())
            self.vq_rot.load_state_dict(load_dict["codebook_rotation"].item())
            scale_codes = self.vq_scale.get_codes_from_indices(torch.from_numpy(scale).cuda().reshape(-1,1,load_dict["rvq_info"][0]).long())
            scale = self.vq_scale.project_out(reduce(scale_codes, 'q ... -> ...', 'sum'))
            rotation_codes = self.vq_rot.get_codes_from_indices(torch.from_numpy(rotation).cuda().reshape(-1,1,load_dict["rvq_info"][0]).long())
            rotation = self.vq_rot.project_out(reduce(rotation_codes, 'q ... -> ...', 'sum'))

            self._xyz = nn.Parameter(torch.from_numpy(load_dict["xyz"]).cuda().float().requires_grad_(True))
            self._opacity = nn.Parameter(torch.from_numpy(load_dict["opacity"]).reshape(-1,1).cuda().float().requires_grad_(True))
            self._scaling = nn.Parameter(scale.squeeze(1).requires_grad_(True))
            self._rotation = nn.Parameter(rotation.squeeze(1).requires_grad_(True))
            self.recolor.params = nn.Parameter(torch.from_numpy(load_dict["hash"]).cuda().half().requires_grad_(True))
            self.mlp_head.params = nn.Parameter(torch.from_numpy(load_dict["mlp"]).cuda().half().requires_grad_(True))
        else:
            self.load_ply(path)

    def load_ply(self, path):
        print("Loading ", path+".ply")
        plydata = PlyData.read(path+".ply")

        xyz = np.stack((np.asarray(plydata.elements[0]["x"]),
                        np.asarray(plydata.elements[0]["y"]),
                        np.asarray(plydata.elements[0]["z"])),  axis=1)
        opacities = np.asarray(plydata.elements[0]["opacity"])[..., np.newaxis]

        scale_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("scale_")]
        scale_names = sorted(scale_names, key = lambda x: int(x.split('_')[-1]))
        scales = np.zeros((xyz.shape[0], len(scale_names)))
        for idx, attr_name in enumerate(scale_names):
            scales[:, idx] = np.asarray(plydata.elements[0][attr_name])

        rot_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("rot")]
        rot_names = sorted(rot_names, key = lambda x: int(x.split('_')[-1]))
        rots = np.zeros((xyz.shape[0], len(rot_names)))
        for idx, attr_name in enumerate(rot_names):
            rots[:, idx] = np.asarray(plydata.elements[0][attr_name])

        # 加载特征
        feature_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("feature_")]
        feature_names = sorted(feature_names, key = lambda x: int(x.split('_')[-1]))
        if feature_names:  # 如果存在特征
            features = np.zeros((xyz.shape[0], len(feature_names)))
            for idx, attr_name in enumerate(feature_names):
                features[:, idx] = np.asarray(plydata.elements[0][attr_name])
            # 保存为模型属性
            self._feature = torch.tensor(features, dtype=torch.float, device="cuda")

        self._xyz = nn.Parameter(torch.tensor(xyz, dtype=torch.float, device="cuda").requires_grad_(True))
        self._opacity = nn.Parameter(torch.tensor(opacities, dtype=torch.float, device="cuda").requires_grad_(True))
        self._scaling = nn.Parameter(torch.tensor(scales, dtype=torch.float, device="cuda").requires_grad_(True))
        self._rotation = nn.Parameter(torch.tensor(rots, dtype=torch.float, device="cuda").requires_grad_(True))

        self.active_sh_degree = self.max_sh_degree

        torch.nn.ModuleList([self.recolor, self.mlp_head]).load_state_dict(torch.load(path +".pth"))

    def replace_tensor_to_optimizer(self, tensor, name):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] == name:
                stored_state = self.optimizer.state.get(group['params'][0], None)
                stored_state["exp_avg"] = torch.zeros_like(tensor)
                stored_state["exp_avg_sq"] = torch.zeros_like(tensor)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(tensor.requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def _prune_optimizer(self, mask):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter((group["params"][0][mask].requires_grad_(True)))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(group["params"][0][mask].requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def prune_points(self, mask):
        valid_points_mask = ~mask
        optimizable_tensors = self._prune_optimizer(valid_points_mask)

        self._xyz = optimizable_tensors["xyz"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]
        self._mask = optimizable_tensors["mask"]

        self.xyz_gradient_accum = self.xyz_gradient_accum[valid_points_mask]

        self.denom = self.denom[valid_points_mask]
        self.max_radii2D = self.max_radii2D[valid_points_mask]

    def cat_tensors_to_optimizer(self, tensors_dict):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            assert len(group["params"]) == 1
            extension_tensor = tensors_dict[group["name"]]
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:

                stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0)
                stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)), dim=0)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]

        return optimizable_tensors

    def densification_postfix(self, new_xyz, new_opacities, new_scaling, new_rotation, new_mask):
        d = {"xyz": new_xyz,
        "opacity": new_opacities,
        "scaling" : new_scaling,
        "rotation" : new_rotation,
        "mask": new_mask}

        optimizable_tensors = self.cat_tensors_to_optimizer(d)
        self._xyz = optimizable_tensors["xyz"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]
        self._mask = optimizable_tensors["mask"]

        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def densify_and_split(self, grads, grad_threshold, scene_extent, N=2):
        n_init_points = self.get_xyz.shape[0]
        # Extract points that satisfy the gradient condition
        padded_grad = torch.zeros((n_init_points), device="cuda")
        padded_grad[:grads.shape[0]] = grads.squeeze()
        selected_pts_mask = torch.where(padded_grad >= grad_threshold, True, False)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values > self.percent_dense*scene_extent)
        
        stds = self.get_scaling[selected_pts_mask].repeat(N,1)
        means =torch.zeros((stds.size(0), 3),device="cuda")
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(self._rotation[selected_pts_mask]).repeat(N,1,1)
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask].repeat(N, 1)
        new_scaling = self.scaling_inverse_activation(self.get_scaling[selected_pts_mask].repeat(N,1) / (0.8*N))
        new_rotation = self._rotation[selected_pts_mask].repeat(N,1)
        new_opacity = self._opacity[selected_pts_mask].repeat(N,1)
        new_mask = self._mask[selected_pts_mask].repeat(N,1)

        self.densification_postfix(new_xyz, new_opacity, new_scaling, new_rotation, new_mask)

        prune_filter = torch.cat((selected_pts_mask, torch.zeros(N * selected_pts_mask.sum(), device="cuda", dtype=bool)))
        self.prune_points(prune_filter)

    def densify_and_clone(self, grads, grad_threshold, scene_extent):
        # Extract points that satisfy the gradient condition
        selected_pts_mask = torch.where(torch.norm(grads, dim=-1) >= grad_threshold, True, False)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values <= self.percent_dense*scene_extent)
        
        new_xyz = self._xyz[selected_pts_mask]
        new_opacities = self._opacity[selected_pts_mask]
        new_scaling = self._scaling[selected_pts_mask]
        new_rotation = self._rotation[selected_pts_mask]
        new_mask = self._mask[selected_pts_mask]

        self.densification_postfix(new_xyz, new_opacities, new_scaling, new_rotation, new_mask)

    def densify_and_prune(self, max_grad, min_opacity, extent, max_screen_size):
        grads = self.xyz_gradient_accum / self.denom
        grads[grads.isnan()] = 0.0
        
        self.densify_and_clone(grads, max_grad, extent)
        self.densify_and_split(grads, max_grad, extent)

        prune_mask = torch.logical_or((torch.sigmoid(self._mask) <= 0.01).squeeze(),(self.get_opacity < min_opacity).squeeze())
        if max_screen_size:
            big_points_vs = self.max_radii2D > max_screen_size
            big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * extent
            prune_mask = torch.logical_or(torch.logical_or(prune_mask, big_points_vs), big_points_ws)
        self.prune_points(prune_mask)
        torch.cuda.empty_cache()
    
    def mask_prune(self):
        prune_mask = (torch.sigmoid(self._mask) <= 0.01).squeeze()
        self.prune_points(prune_mask)
        torch.cuda.empty_cache()

    def post_quant(self, param, prune=False, track_watermarks=False): # 没测过，不知道能不能跑通
        max_val = torch.amax(param)
        min_val = torch.amin(param)

        # 如果需要追踪水印点，记录原始值
        tracked_values = {}
        if track_watermarks and self.watermarked_points:
            # 对每一组水印点分别追踪
            for i, indices in enumerate(self.watermarked_points):
                # 将列表转换为tensor以便索引
                indices_tensor = torch.tensor(indices, device=param.device)
                tracked_values[f'group_{i}'] = {
                    'indices': indices,
                    'original': param[indices_tensor].clone()
                }

        if prune:
            param = param*(torch.abs(param) > 0.1)
        param = (param - min_val)/(max_val - min_val)
        quant = torch.round(param * 255.0)
        out = (max_val - min_val)*quant/255.0 + min_val

        # 如果在追踪水印点，记录量化后的值
        if track_watermarks and tracked_values:
            for group_key in tracked_values:
                indices_tensor = torch.tensor(tracked_values[group_key]['indices'], device=out.device)
                tracked_values[group_key]['quantized'] = out[indices_tensor]
            
            return (torch.nn.Parameter(out), quant, torch.tensor([min_val, max_val]), tracked_values)
        
        return torch.nn.Parameter(out), quant, torch.tensor([min_val, max_val]), None
    
    def huffman_encode(self, param):
        input_code_list = param.view(-1).tolist()
        unique, counts = np.unique(input_code_list, return_counts=True)
        num_freq = dict(zip(unique, counts))

        codec = HuffmanCodec.from_data(input_code_list)

        sym_bit_dict = {}
        for k, v in codec.get_code_table().items():
            sym_bit_dict[k] = v[0]
        total_bits = 0
        for num, freq in num_freq.items():
            total_bits += freq * sym_bit_dict[num]
        total_mb = total_bits/8/10**6
        
        return total_mb, codec.encode(input_code_list), codec.get_code_table()

    def convert_watermark_values(self, string):
        padded_string = string + '0' * (8 - (len(string) % 8) if len(string) % 8 != 0 else 0)
        
        chunks = [padded_string[i:i+8] for i in range(0, len(padded_string), 8)]
        
        decimals = []
        for chunk in chunks:
            decimal = float('0.' + chunk)
            decimals.append(decimal)
        
        return decimals

    def final_prune(self, compress=False):
        prune_mask = (torch.sigmoid(self._mask) <= 0.01).squeeze()
    
        points_to_delete = torch.where(prune_mask)[0]
        
        mask_values = torch.sigmoid(self._mask[points_to_delete]).squeeze()
        
        print(f"points_to_delete_length: {len(points_to_delete)}")

        if points_to_delete.shape[0] > 50:
            _, top_indices = torch.topk(mask_values, 50, largest=False)
            watermark_indices = points_to_delete[top_indices]
        else:
            watermark_indices = points_to_delete

        prune_mask[watermark_indices] = False

        self.prune_points(prune_mask)

        if compress:
            self.sort_morton()

        print(f"watermark_value to be encoded: {self.watermark_value}")
        watermark_values = self.convert_watermark_values(self.watermark_value)
        watermarked_mask = (torch.sigmoid(self._mask) <= 0.01).squeeze()
        points_to_watermark = torch.where(watermarked_mask)[0]

        # Calculate points per group
        num_groups = len(watermark_values)
        points_per_group = len(points_to_watermark) // num_groups
        remainder = len(points_to_watermark) % num_groups

        self.watermarked_points = {}
        start_idx = 0

        for i, watermark_value in enumerate(watermark_values):
            group_size = points_per_group + (1 if i < remainder else 0)
            end_idx = start_idx + group_size
            group_indices = points_to_watermark[start_idx:end_idx]
            watermark_opacity = torch.tensor(watermark_value, dtype=torch.float32, device="cuda")

            self._opacity[group_indices] = watermark_opacity
            self.watermarked_points[f'group_{i}'] = {
                'indices': group_indices.cpu().tolist(),
                'opacity_value': watermark_value
            }
            
            print(f"Group {i}: {len(group_indices)} points, opacity: {watermark_opacity:.8f}")
            start_idx = end_idx

        for m in self.vq_scale.layers:
            m.training = False
        for m in self.vq_rot.layers: 
            m.training = False

        self._xyz = self._xyz.clone().half().float()
        self._scaling, self.sca_idx, _ = self.vq_scale(self.get_scaling.unsqueeze(1))
        self._rotation, self.rot_idx, _ = self.vq_rot(self.get_rotation.unsqueeze(1))
        self._scaling = self._scaling.squeeze()
        self._rotation = self._rotation.squeeze()

        # print(f"self._scaling_before: {self._scaling[points_to_watermark]}")

        position_mb = self._xyz.shape[0]*3*16/8/10**6
        scale_mb = self._xyz.shape[0]*self.rvq_bit*self.rvq_num/8/10**6 + 2**self.rvq_bit*self.rvq_num*3*32/8/10**6
        rotation_mb = self._xyz.shape[0]*self.rvq_bit*self.rvq_num/8/10**6 + 2**self.rvq_bit*self.rvq_num*4*32/8/10**6
        opacity_mb = self._xyz.shape[0]*16/8/10**6
        hash_mb = self.recolor.params.shape[0]*16/8/10**6
        mlp_mb = self.mlp_head.params.shape[0]*16/8/10**6
        sum_mb = position_mb+scale_mb+rotation_mb+opacity_mb+hash_mb+mlp_mb
        
        mb_str = "Storage\nposition: "+str(position_mb)+"\nscale: "+str(scale_mb)+"\nrotation: "+str(rotation_mb)+"\nopacity: "+str(opacity_mb)+"\nhash: "+str(hash_mb)+"\nmlp: "+str(mlp_mb)+"\ntotal: "+str(sum_mb)+" MB"
        

        tracked_values = None

        if compress:
            self._opacity, self.quant_opa, self.minmax_opa, tracked_values = self.post_quant(param=self.get_opacity, prune=False, track_watermarks=True)
            self.recolor.params, self.quant_hash, self.minmax_hash,_ = self.post_quant(param=self.recolor.params, prune=True, track_watermarks=False)
        
            scale_mb, self.huf_sca, self.tab_sca = self.huffman_encode(self.sca_idx) 
            scale_mb += 2**self.rvq_bit*self.rvq_num*3*32/8/10**6
            rotation_mb, self.huf_rot, self.tab_rot = self.huffman_encode(self.rot_idx)
            rotation_mb += 2**self.rvq_bit*self.rvq_num*4*32/8/10**6
            opacity_mb, self.huf_opa, self.tab_opa = self.huffman_encode(self.quant_opa)
            hash_mb, self.huf_hash, self.tab_hash = self.huffman_encode(self.quant_hash)
            mlp_mb = self.mlp_head.params.shape[0]*16/8/10**6
            sum_mb = position_mb+scale_mb+rotation_mb+opacity_mb+hash_mb+mlp_mb
            
            mb_str = mb_str+"\n\nAfter PP\nposition: "+str(position_mb)+"\nscale: "+str(scale_mb)+"\nrotation: "+str(rotation_mb)+"\nopacity: "+str(opacity_mb)+"\nhash: "+str(hash_mb)+"\nmlp: "+str(mlp_mb)+"\ntotal: "+str(sum_mb)+" MB"
        else:
            tracked_values = {}
            for group_key, group_data in self.watermarked_points.items():
                indices = group_data['indices']
                indices_tensor = torch.tensor(indices, device=self._opacity.device)
                tracked_values[group_key] = {
                    'indices': indices,
                    'original': self._opacity[indices_tensor].clone()
                }

            opacities = self._opacity.clone()
            watermark_mask = torch.zeros_like(opacities, dtype=torch.bool)
            
            all_watermarked_indices = []
            for group_data in self.watermarked_points.values():
                all_watermarked_indices.extend(group_data['indices'])
            watermark_mask[all_watermarked_indices] = True

            non_watermark_opacities = self.get_opacity[~watermark_mask].clone().half().float() # 压缩非水印点
            self._opacity = opacities.clone()
            self._opacity[~watermark_mask] = non_watermark_opacities

            for group_key in tracked_values:
                indices_tensor = torch.tensor(tracked_values[group_key]['indices'], device=self._opacity.device)
                tracked_values[group_key]['quantized'] = self._opacity[indices_tensor]

        # print(f"is_compress: {compress}")
        # if tracked_values:
        #     print("Tracked Watermark Values:")
        #     for group_key, group_data in tracked_values.items():
        #         print(f"{group_key}:")
        #         print(f"  indices: {group_data['indices']}")
        #         print(f"  original[0]: {group_data['original'][0].item():.8f}")
        #         print(f"  quantized[0]: {group_data['quantized'][0].item():.8f}")
        #         print()
        self.watermarked_points = tracked_values

        torch.cuda.empty_cache()
        return mb_str
    
    def precompute(self, compute_feature=True):
        xyz = self.contract_to_unisphere(self.get_xyz.half(), torch.tensor([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0], device='cuda'))
        if compute_feature:
            features = self.recolor(xyz)
            if self.watermarked_points:
                
                features_list = []
                all_watermarked_indices = []
                for group_data in self.watermarked_points.values():
                    all_watermarked_indices.extend(group_data['indices'])

                watermark_mask = torch.zeros(len(xyz), dtype=torch.bool, device='cuda')
                watermark_mask[all_watermarked_indices] = True
                
                non_watermark_features = features[~watermark_mask]
                features_list.append((~watermark_mask, non_watermark_features))

                for i, (group_key, group_data) in enumerate(self.watermarked_points.items()):
                        
                    self.watermark_recolor = tcnn.Encoding(
                        n_input_dims=3,
                        encoding_config={
                            "otype": "HashGrid",
                            "n_levels": 16,
                            "n_features_per_level": 2,
                            "log2_hashmap_size": self.max_hashmap,
                            "base_resolution": 16,
                            "per_level_scale": self.watermark_scales[i],
                        },
                    )
                       
                    group_indices = torch.tensor(group_data['indices'], device='cuda')
                    group_xyz = xyz[group_indices]
                    group_features = self.watermark_recolor(group_xyz)
                    
                    group_mask = torch.zeros(len(xyz), dtype=torch.bool, device='cuda')
                    group_mask[group_indices] = True
                    
                    features_list.append((group_mask, group_features))

                final_features = torch.zeros_like(features)
                for mask, group_features in features_list:
                    final_features[mask] = group_features

                self._feature = final_features
            else:
                self._feature = features

        torch.cuda.empty_cache()

    def add_densification_stats(self, viewspace_point_tensor, update_filter):
        self.xyz_gradient_accum[update_filter] += torch.norm(viewspace_point_tensor.grad[update_filter,:2], dim=-1, keepdim=True)
        self.denom[update_filter] += 1
        
    def contract_to_unisphere(self,
        x: torch.Tensor,
        aabb: torch.Tensor,
        ord: int = 2,
        eps: float = 1e-6,
        derivative: bool = False,
    ):
        aabb_min, aabb_max = torch.split(aabb, 3, dim=-1)
        x = (x - aabb_min) / (aabb_max - aabb_min)
        x = x * 2 - 1  # aabb is at [-1, 1]
        mag = torch.linalg.norm(x, ord=ord, dim=-1, keepdim=True)
        mask = mag.squeeze(-1) > 1

        if derivative:
            dev = (2 * mag - 1) / mag**2 + 2 * x**2 * (
                1 / mag**3 - (2 * mag - 1) / mag**4
            )
            dev[~mask] = 1.0
            dev = torch.clamp(dev, min=eps)
            return dev
        else:
            x[mask] = (2 - 1 / mag[mask]) * (x[mask] / mag[mask])
            x = x / 4 + 0.5  # [-inf, inf] is at [0, 1]
            return x

    def sort_morton(self):
        with torch.no_grad():
            xyz_q = (
                (2**21 - 1)
                * (self._xyz - self._xyz.min(0).values)
                / (self._xyz.max(0).values - self._xyz.min(0).values)
            ).long()
            order = mortonEncode(xyz_q).sort().indices
            
            self._xyz = nn.Parameter(self._xyz[order], requires_grad=True)
            self._opacity = nn.Parameter(self._opacity[order], requires_grad=True)
            self._scaling = nn.Parameter(self._scaling[order], requires_grad=True)
            self._rotation = nn.Parameter(self._rotation[order], requires_grad=True)