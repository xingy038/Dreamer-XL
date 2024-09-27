from audioop import mul
from transformers import CLIPTextModel, CLIPTokenizer, logging
from diffusers import StableDiffusionPipeline, DiffusionPipeline, DDPMScheduler, DDIMScheduler, EulerDiscreteScheduler, \
                      EulerAncestralDiscreteScheduler, DPMSolverMultistepScheduler, ControlNetModel, \
                      DDIMInverseScheduler, UNet2DConditionModel, StableDiffusionXLPipeline, StableDiffusionXLImg2ImgPipeline, \
                      AutoencoderKL
from diffusers.utils.import_utils import is_xformers_available
from os.path import isfile
from pathlib import Path
import os
import random

from huggingface_hub import hf_hub_download
from safetensors.torch import load_file

# from huggingface_hub import login
# login()

import torchvision.transforms as T
# suppress partial model loading warning
logging.set_verbosity_error()

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as T
from torchvision.utils import save_image
from torch.cuda.amp import custom_bwd, custom_fwd
from .perpneg_utils import weighted_perpendicular_aggregator

from .sd_step import *
def rgb2sat(img, T=None):
    max_ = torch.max(img, dim=1, keepdim=True).values + 1e-5
    min_ = torch.min(img, dim=1, keepdim=True).values
    sat = (max_ - min_) / max_
    if T is not None:
        sat = (1 - T) * sat
    return sat

class SpecifyGradient(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(ctx, input_tensor, gt_grad):
        ctx.save_for_backward(gt_grad)
        # we return a dummy value 1, which will be scaled by amp's scaler so we get the scale in backward.
        return torch.ones([1], device=input_tensor.device, dtype=input_tensor.dtype)

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_scale):
        gt_grad, = ctx.saved_tensors
        gt_grad = gt_grad * grad_scale
        return gt_grad, None

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    #torch.backends.cudnn.deterministic = True
    #torch.backends.cudnn.benchmark = True

class StableDiffusion(nn.Module):
    def __init__(self, device, fp16, vram_O, t_range=[0.02, 0.98], max_t_range=0.98, num_train_timesteps=None, 
                 ddim_inv=False, use_control_net=False, textual_inversion_path = None, 
                 LoRA_path = None, guidance_opt=None):
        super().__init__()

        self.device = device
        self.precision_t = torch.float16 if fp16 else torch.float32

        print(f'[INFO] loading SDXL...')

        model_key = guidance_opt.model_key

        cache_dir = "pretrained/SDXL"
        general_kwargs = {
            "cache_dir": cache_dir,
            "torch_dtype": self.precision_t,
            "use_safetensors": True,
            "variant": "fp16",
            # "local_files_only": True,
            # "use_auth_token": token,
        }
        vae = AutoencoderKL.from_pretrained(
            model_key,
            # "madebyollin/sdxl-vae-fp16-fix",
            # "stabilityai/sdxl-vae",
            # local_files_only=True,
            subfolder="vae",
            torch_dtype=torch.float32
        )
        
        pipe = StableDiffusionXLPipeline.from_pretrained(model_key, vae=vae, **general_kwargs)


        self.ism = not guidance_opt.sds
        self.scheduler = DDIMScheduler.from_pretrained(model_key, subfolder="scheduler", torch_dtype=self.precision_t)
        self.sche_func = ddim_step

        if use_control_net:
            controlnet_model_key = guidance_opt.controlnet_model_key
            self.controlnet_depth = ControlNetModel.from_pretrained(controlnet_model_key,torch_dtype=self.precision_t).to(device)

        if vram_O:
            pipe.enable_sequential_cpu_offload()
            pipe.enable_vae_slicing()
            pipe.unet.to(memory_format=torch.channels_last)
            pipe.enable_attention_slicing(1)
            pipe.enable_model_cpu_offload()

        pipe.enable_xformers_memory_efficient_attention()
    
        pipe = pipe.to(self.device)
        if textual_inversion_path is not None:
            pipe.load_textual_inversion(textual_inversion_path)
            print("load textual inversion in:.{}".format(textual_inversion_path))
        
        if LoRA_path is not None:
            from lora_diffusion import tune_lora_scale, patch_pipe
            print("load lora in:.{}".format(LoRA_path))
            patch_pipe(
                pipe,
                LoRA_path,
                patch_text=True,
                patch_ti=True,
                patch_unet=True,
            )
            tune_lora_scale(pipe.unet, 1.00)
            tune_lora_scale(pipe.text_encoder, 1.00)

        self.pipe = pipe
        self.vae = pipe.vae
        self.tokenizer = pipe.tokenizer
        self.tokenizer_2 = pipe.tokenizer_2
        self.text_encoder = pipe.text_encoder
        self.text_encoder_2 = pipe.text_encoder_2
        self.unet = pipe.unet
        
        self.num_train_timesteps = num_train_timesteps if num_train_timesteps is not None else self.scheduler.config.num_train_timesteps        
        self.scheduler.set_timesteps(self.num_train_timesteps, device=device)

        self.timesteps = torch.flip(self.scheduler.timesteps, dims=(0, ))
        self.min_step = int(self.num_train_timesteps * t_range[0])
        self.max_step = int(self.num_train_timesteps * t_range[1])
        self.warmup_step = int(self.num_train_timesteps*(max_t_range-t_range[1]))
        
        self.add_time_ids_neg = None

        self.noise_temp = None
        self.noise_gen = torch.Generator(self.device)
        self.noise_gen.manual_seed(guidance_opt.noise_seed)

        self.alphas = self.scheduler.alphas_cumprod.to(self.device) # for convenience
        self.rgb_latent_factors = torch.tensor([
                    # R       G       B
                    [ 0.298,  0.207,  0.208],
                    [ 0.187,  0.286,  0.173],
                    [-0.158,  0.189,  0.264],
                    [-0.184, -0.271, -0.473]
                ], device=self.device)
        

        print(f'[INFO] loaded stable diffusion!')

    def augmentation(self, *tensors):
        augs = T.Compose([
                        T.RandomHorizontalFlip(p=0.5),
                    ])
        
        channels = [ten.shape[1] for ten in tensors]
        tensors_concat = torch.concat(tensors, dim=1)
        tensors_concat = augs(tensors_concat)

        results = []
        cur_c = 0
        for i in range(len(channels)):
            results.append(tensors_concat[:, cur_c:cur_c + channels[i], ...])
            cur_c += channels[i]
        return (ten for ten in results)

    def add_noise_with_cfg(self, latents, noise, 
                           ind_t, ind_prev_t, text_embeddings_0=None,
                           text_embeddings=None, cfg=1.0, 
                           delta_t=1, inv_steps=1,
                           is_noisy_latent=False,
                           eta=0.0):

        text_embeddings = text_embeddings.to(self.precision_t)
        if self.add_time_ids_neg is not None:
            add_time_ids, _ = self.add_time_ids_neg.reshape(self.add_time_ids_neg.shape[0] // 2, -1).chunk(2)
            add_time_ids = add_time_ids.reshape(add_time_ids.shape[0] * 2, -1)
        else:
            add_time_ids, _ = self.add_time_ids.chunk(2)
        if cfg <= 1.0:
            uncond_text_embeddings_0 = text_embeddings_0.reshape(2, -1, text_embeddings_0.shape[-2], text_embeddings_0.shape[-1])[1]

        unet = self.unet

        if is_noisy_latent:
            prev_noisy_lat = latents
        else:
            prev_noisy_lat = self.scheduler.add_noise(latents, noise, self.timesteps[ind_prev_t])

        cur_ind_t = ind_prev_t
        cur_noisy_lat = prev_noisy_lat

        pred_scores = []

        for i in range(inv_steps):
            # pred noise
            cur_noisy_lat_ = self.scheduler.scale_model_input(cur_noisy_lat, self.timesteps[cur_ind_t]).to(self.precision_t)
            
            if cfg > 1.0:
                latent_model_input = torch.cat([cur_noisy_lat_, cur_noisy_lat_])
                timestep_model_input = self.timesteps[cur_ind_t].reshape(1, 1).repeat(latent_model_input.shape[0], 1).reshape(-1)
                unet_output = unet(latent_model_input, timestep_model_input, 
                                encoder_hidden_states=text_embeddings_0, added_cond_kwargs={"text_embeds": text_embeddings.repeat(2, 1), "time_ids": add_time_ids}).sample
                
                uncond, cond = torch.chunk(unet_output, chunks=2)
                
                unet_output = cond + cfg * (uncond - cond) # reverse cfg to enhance the distillation
            else:
                timestep_model_input = self.timesteps[cur_ind_t].reshape(1, 1).repeat(cur_noisy_lat_.shape[0], 1).reshape(-1)
                unet_output = unet(cur_noisy_lat_, timestep_model_input, 
                                    encoder_hidden_states=uncond_text_embeddings_0, added_cond_kwargs={"text_embeds": text_embeddings.repeat(2, 1), "time_ids": add_time_ids}).sample

            pred_scores.append((cur_ind_t, unet_output))

            next_ind_t = min(cur_ind_t + delta_t, ind_t)
            cur_t, next_t = self.timesteps[cur_ind_t], self.timesteps[next_ind_t]
            delta_t_ = next_t-cur_t if isinstance(self.scheduler, DDIMScheduler) else next_ind_t-cur_ind_t

            cur_noisy_lat = self.sche_func(self.scheduler, unet_output, cur_t, cur_noisy_lat, -delta_t_, eta).prev_sample
            cur_ind_t = next_ind_t

            del unet_output
            torch.cuda.empty_cache()

            if cur_ind_t == ind_t:
                break

        return prev_noisy_lat, cur_noisy_lat, pred_scores[::-1]


    
    @torch.no_grad()
    def get_text_embeds(self, prompt):
        # prompt, negative_prompt: [str]

        # Define tokenizers and text encoders
        tokenizers = [self.tokenizer, self.tokenizer_2] if self.tokenizer is not None else [self.tokenizer_2]
        text_encoders = (
            [self.text_encoder, self.text_encoder_2] if self.text_encoder is not None else [self.text_encoder_2]
        )

        prompt_embeds_list = []
        for tokenizer, text_encoder in zip(tokenizers, text_encoders):
            text_inputs = tokenizer(prompt, padding="max_length", max_length=tokenizer.model_max_length, truncation=True, return_tensors="pt")

            with torch.no_grad():
                prompt_embeds = text_encoder(text_inputs.input_ids.to(self.device), output_hidden_states=True)
                pooled_prompt_embeds = prompt_embeds[0]
                prompt_embeds = prompt_embeds.hidden_states[-2]
                prompt_embeds_list.append(prompt_embeds)

        prompt_embeds = torch.concat(prompt_embeds_list, dim=-1)
        return prompt_embeds, pooled_prompt_embeds
    
    def _get_add_time_ids(self, original_size, crops_coords_top_left, target_size, dtype):
        add_time_ids = list(original_size + crops_coords_top_left + target_size)
        add_time_ids = torch.tensor([add_time_ids], dtype=dtype)
        return add_time_ids

    def train_step_perpneg(self, text_embeddings_0, text_embeddings, pred_rgb, pred_depth=None, pred_alpha=None,
                           grad_scale=1,use_control_net=False,
                           save_folder:Path=None, iteration=0, warm_up_rate = 0, weights = 0, 
                           resolution=(512, 512), guidance_opt=None,as_latent=False, embedding_inverse = None, embedding_inverse_0 = None):


        # flip aug
        pred_rgb, pred_depth, pred_alpha = self.augmentation(pred_rgb, pred_depth, pred_alpha)

        B = pred_rgb.shape[0]
        K = text_embeddings.shape[0] - 1
        
        add_time_ids = self._get_add_time_ids(resolution, (0, 0), resolution, dtype=text_embeddings.dtype).repeat_interleave(B, dim=0)
        self.add_time_ids = torch.cat([add_time_ids, add_time_ids], dim=0).to(self.device)
        self.add_time_ids_neg = self.add_time_ids

        if as_latent:      
            latents, _ = self.encode_imgs(pred_depth.repeat(1,3,1,1))
        else:
            latents, _ = self.encode_imgs(pred_rgb)
        # timestep ~ U(0.02, 0.98) to avoid very high/low noise level
        
        weights = weights.reshape(-1)
        noise = torch.randn((latents.shape[0], 4, resolution[0] // 8, resolution[1] // 8, ), dtype=latents.dtype, device=latents.device, generator=self.noise_gen) + 0.1 * torch.randn((1, 4, 1, 1), device=latents.device).repeat(latents.shape[0], 1, 1, 1)
   
        inverse_text_embeddings_0 = embedding_inverse_0.unsqueeze(1).repeat(1, B, 1, 1).reshape(-1, embedding_inverse_0.shape[-2], embedding_inverse_0.shape[-1])
        
        text_embeddings_0 = text_embeddings_0[:, :, ...]
        text_embeddings_0 = text_embeddings_0.reshape(-1, text_embeddings_0.shape[-2], text_embeddings_0.shape[-1]) # make it k+1, c * t, ...

        if guidance_opt.annealing_intervals:
            current_delta_t =  int(guidance_opt.delta_t + np.ceil((warm_up_rate)*(guidance_opt.delta_t_start - guidance_opt.delta_t)))
        else:
            current_delta_t =  guidance_opt.delta_t

        ind_t = torch.randint(self.min_step, self.max_step + int(self.warmup_step*warm_up_rate), (1, ), dtype=torch.long, generator=self.noise_gen, device=self.device)[0]
        ind_prev_t = max(ind_t - current_delta_t, torch.ones_like(ind_t) * 0)
        
        gamma = guidance_opt.gamma
        gamma_tensor = torch.tensor(gamma)
        mu_ratio = torch.sqrt(1 - gamma_tensor ** 2)
        ind_mu_t = ind_prev_t + (ind_t - ind_prev_t) * mu_ratio
        ind_mu_t = torch.round(ind_mu_t).long()

        t = self.timesteps[ind_t]
        prev_t = self.timesteps[ind_prev_t]

        with torch.no_grad():
            # step unroll via ddim inversion
            if not self.ism:
                prev_latents_noisy = self.scheduler.add_noise(latents, noise, prev_t)
                latents_noisy = self.scheduler.add_noise(latents, noise, t)
                target = noise
            else:
                # Step 1: sample x_s with larger steps
                xs_delta_t = guidance_opt.xs_delta_t if guidance_opt.xs_delta_t is not None else current_delta_t
                xs_inv_steps = guidance_opt.xs_inv_steps if guidance_opt.xs_inv_steps is not None else int(np.ceil(ind_prev_t / xs_delta_t))
                starting_ind = max(ind_prev_t - xs_delta_t * xs_inv_steps, torch.ones_like(ind_t) * 0)

                _, prev_latents_noisy, pred_scores_xs = self.add_noise_with_cfg(latents, noise, ind_prev_t, starting_ind, inverse_text_embeddings_0,  embedding_inverse, 
                                                                                guidance_opt.denoise_guidance_scale, xs_delta_t, xs_inv_steps, eta=guidance_opt.xs_eta)
                # Step 2: sample x_t
                _, latents_noisy_mu, pred_scores_mu = self.add_noise_with_cfg(prev_latents_noisy, noise, ind_mu_t,
                                                                              ind_prev_t,
                                                                              inverse_text_embeddings_0, embedding_inverse, 
                                                                              guidance_opt.denoise_guidance_scale,
                                                                              xs_delta_t, 1, is_noisy_latent=True)

                _, _, pred_scores_mu2t = self.add_noise_with_cfg(latents_noisy_mu, noise, ind_t,
                                                                                  ind_mu_t,
                                                                                  inverse_text_embeddings_0,  embedding_inverse,
                                                                                  guidance_opt.denoise_guidance_scale,
                                                                                  xs_delta_t, 1, is_noisy_latent=True)

                _, latents_noisy, pred_scores_xt = self.add_noise_with_cfg(prev_latents_noisy, noise, ind_t, ind_prev_t,
                                                                           inverse_text_embeddings_0,  embedding_inverse,
                                                                           guidance_opt.denoise_guidance_scale,
                                                                           current_delta_t, 1, is_noisy_latent=True)

                pred_scores = pred_scores_xt + pred_scores_xs
                pred_scores_mu2t = pred_scores_mu2t + pred_scores_mu + pred_scores_xs
                target = pred_scores[0][1]
                target_mu2t = pred_scores_mu2t[0][1]


        with torch.no_grad():
            latent_model_input = latents_noisy[None, :, ...].repeat(1 + K, 1, 1, 1, 1).reshape(-1, 4, resolution[0] // 8, resolution[1] // 8, )
            tt = t.reshape(1, 1).repeat(latent_model_input.shape[0], 1).reshape(-1)

            latent_model_input = self.scheduler.scale_model_input(latent_model_input, tt[0])
            if use_control_net:
                pred_depth_input = pred_depth_input[None, :, ...].repeat(1 + K, 1, 3, 1, 1).reshape(-1, 3, 512, 512).half()
                down_block_res_samples, mid_block_res_sample = self.controlnet_depth(
                    latent_model_input,
                    tt,
                    encoder_hidden_states=text_embeddings,
                    controlnet_cond=pred_depth_input,
                    return_dict=False,
                )
                unet_output = self.unet(latent_model_input, tt, encoder_hidden_states=text_embeddings,
                                    down_block_additional_residuals=down_block_res_samples,
                                    mid_block_additional_residual=mid_block_res_sample).sample
            else:
                unet_output = self.unet(latent_model_input.to(self.precision_t), tt.to(self.precision_t), encoder_hidden_states=text_embeddings_0.to(self.precision_t), added_cond_kwargs={"text_embeds": text_embeddings.reshape(-1, text_embeddings.shape[-1]), "time_ids": self.add_time_ids.repeat(2, 1)}).sample

            unet_output = unet_output.reshape(1 + K, -1, 4, resolution[0] // 8, resolution[1] // 8, )
            noise_pred_uncond, noise_pred_text = unet_output[:1].reshape(-1, 4, resolution[0] // 8, resolution[1] // 8, ), unet_output[1:].reshape(-1, 4, resolution[0] // 8, resolution[1] // 8, )
            delta_noise_preds = noise_pred_text - noise_pred_uncond.repeat(K, 1, 1, 1)
            delta_DSD = weighted_perpendicular_aggregator(delta_noise_preds, weights, B)     

        pred_noise = noise_pred_uncond + guidance_opt.guidance_scale * delta_DSD
        w = lambda alphas: (((1 - alphas) / alphas) ** 0.5)

        grad_mu2t = w(self.alphas[t]) * (pred_noise - target_mu2t)

        grad = torch.nan_to_num(grad_scale * grad_mu2t)

        loss = SpecifyGradient.apply(latents, grad)
        
        if iteration % guidance_opt.vis_interval == 0:
            noise_pred_post = noise_pred_uncond + guidance_opt.guidance_scale * delta_DSD    
            lat2rgb = lambda x: torch.clip((x.permute(0,2,3,1) @ self.rgb_latent_factors.to(x.dtype)).permute(0,3,1,2), 0., 1.)
            save_path_iter = os.path.join(save_folder,"iter_{}_step_{}.jpg".format(iteration,prev_t.item()))
            with torch.no_grad():
                pred_x0_latent_sp = pred_original(self.scheduler, noise_pred_uncond, prev_t, prev_latents_noisy)    
                pred_x0_latent_pos = pred_original(self.scheduler, noise_pred_post, prev_t, prev_latents_noisy)        
                pred_x0_pos = self.decode_latents(pred_x0_latent_pos.type(self.precision_t))
                pred_x0_sp = self.decode_latents(pred_x0_latent_sp.type(self.precision_t))

                grad_abs = torch.abs(grad.detach())
                norm_grad  = F.interpolate((grad_abs / grad_abs.max()).mean(dim=1,keepdim=True), (resolution[0], resolution[1]), mode='bilinear', align_corners=False).repeat(1,3,1,1)

                latents_rgb = F.interpolate(lat2rgb(latents), (resolution[0], resolution[1]), mode='bilinear', align_corners=False)
                latents_sp_rgb = F.interpolate(lat2rgb(pred_x0_latent_sp), (resolution[0], resolution[1]), mode='bilinear', align_corners=False)

                viz_images = torch.cat([pred_rgb, 
                                        pred_depth.repeat(1, 3, 1, 1), 
                                        pred_alpha.repeat(1, 3, 1, 1), 
                                        rgb2sat(pred_rgb, pred_alpha).repeat(1, 3, 1, 1),
                                        latents_rgb, latents_sp_rgb, 
                                        norm_grad,
                                        pred_x0_sp, pred_x0_pos],dim=0) 
                save_image(viz_images, save_path_iter)


        return loss


    def train_step(self, text_embeddings_0, text_embeddings, pred_rgb, pred_depth=None, pred_alpha=None,
                    grad_scale=1,use_control_net=False,
                    save_folder:Path=None, iteration=0, warm_up_rate = 0,
                    resolution=(1024, 1024), guidance_opt=None,as_latent=False, embedding_inverse = None, embedding_inverse_0 = None):

        pred_rgb, pred_depth, pred_alpha = self.augmentation(pred_rgb, pred_depth, pred_alpha)

        B = pred_rgb.shape[0]
        K = text_embeddings.shape[0] - 1
        
        add_time_ids = self._get_add_time_ids(resolution, (0, 0), resolution, dtype=text_embeddings.dtype).repeat_interleave(B, dim=0)
        self.add_time_ids = torch.cat([add_time_ids, add_time_ids], dim=0).to(self.device)

        if as_latent:      
            latents,_ = self.encode_imgs(pred_depth.repeat(1,3,1,1))
        else:
            latents,_ = self.encode_imgs(pred_rgb)
        # timestep ~ U(0.02, 0.98) to avoid very high/low noise level

        if self.noise_temp is None:
            self.noise_temp = torch.randn((latents.shape[0], 4, resolution[0] // 8, resolution[1] // 8, ), dtype=latents.dtype, device=latents.device, generator=self.noise_gen) + 0.1 * torch.randn((1, 4, 1, 1), device=latents.device).repeat(latents.shape[0], 1, 1, 1)
        
        if guidance_opt.fix_noise:
            noise = self.noise_temp
        else:
            noise = torch.randn((latents.shape[0], 4, resolution[0] // 8, resolution[1] // 8, ), dtype=latents.dtype, device=latents.device, generator=self.noise_gen) + 0.1 * torch.randn((1, 4, 1, 1), device=latents.device).repeat(latents.shape[0], 1, 1, 1)
        
        text_embeddings_0 = text_embeddings_0[:, :, ...]
        text_embeddings_0 = text_embeddings_0.reshape(-1, text_embeddings_0.shape[-2], text_embeddings_0.shape[-1]) # make it k+1, c * t, ...

        inverse_text_embeddings_0 = embedding_inverse_0.unsqueeze(1).repeat(1, B, 1, 1).reshape(-1, embedding_inverse_0.shape[-2], embedding_inverse_0.shape[-1])

        if guidance_opt.annealing_intervals:
            current_delta_t =  int(guidance_opt.delta_t + (warm_up_rate)*(guidance_opt.delta_t_start - guidance_opt.delta_t))
        else:
            current_delta_t =  guidance_opt.delta_t

        ind_t = torch.randint(self.min_step, self.max_step + int(self.warmup_step*warm_up_rate), (1, ), dtype=torch.long, generator=self.noise_gen, device=self.device)[0]
        ind_prev_t = max(ind_t - current_delta_t, torch.ones_like(ind_t) * 0)
        
        gamma = guidance_opt.gamma
        gamma_tensor = torch.tensor(gamma)
        mu_ratio = torch.sqrt(1 - gamma_tensor ** 2)
        ind_mu_t = ind_prev_t + (ind_t - ind_prev_t) * mu_ratio
        ind_mu_t = torch.round(ind_mu_t).long()

        t = self.timesteps[ind_t]
        prev_t = self.timesteps[ind_prev_t]

        with torch.no_grad():
            # step unroll via ddim inversion
            if not self.ism:
                prev_latents_noisy = self.scheduler.add_noise(latents, noise, prev_t)
                latents_noisy = self.scheduler.add_noise(latents, noise, t)
                target = noise
            else:
                # Step 1: sample x_s with larger steps
                xs_delta_t = guidance_opt.xs_delta_t if guidance_opt.xs_delta_t is not None else current_delta_t
                xs_inv_steps = guidance_opt.xs_inv_steps if guidance_opt.xs_inv_steps is not None else int(np.ceil(ind_prev_t / xs_delta_t))
                starting_ind = max(ind_prev_t - xs_delta_t * xs_inv_steps, torch.ones_like(ind_t) * 0)

                _, prev_latents_noisy, pred_scores_xs = self.add_noise_with_cfg(latents, noise, ind_prev_t, starting_ind, inverse_text_embeddings_0,  embedding_inverse, 
                                                                                guidance_opt.denoise_guidance_scale, xs_delta_t, xs_inv_steps, eta=guidance_opt.xs_eta)
                # Step 2: sample x_t
                _, latents_noisy_mu, pred_scores_mu = self.add_noise_with_cfg(prev_latents_noisy, noise, ind_mu_t,
                                                                              ind_prev_t,
                                                                              inverse_text_embeddings_0, embedding_inverse, 
                                                                              guidance_opt.denoise_guidance_scale,
                                                                              xs_delta_t, 1, is_noisy_latent=True)

                _, _, pred_scores_mu2t = self.add_noise_with_cfg(latents_noisy_mu, noise, ind_t,
                                                                                  ind_mu_t,
                                                                                  inverse_text_embeddings_0,  embedding_inverse,
                                                                                  guidance_opt.denoise_guidance_scale,
                                                                                  xs_delta_t, 1, is_noisy_latent=True)

                _, latents_noisy, pred_scores_xt = self.add_noise_with_cfg(prev_latents_noisy, noise, ind_t, ind_prev_t,
                                                                           inverse_text_embeddings_0,  embedding_inverse,
                                                                           guidance_opt.denoise_guidance_scale,
                                                                           current_delta_t, 1, is_noisy_latent=True)

                pred_scores = pred_scores_xt + pred_scores_xs
                pred_scores_mu2t = pred_scores_mu2t + pred_scores_mu + pred_scores_xs
                target = pred_scores[0][1]
                target_mu2t = pred_scores_mu2t[0][1]


        with torch.no_grad():
            latent_model_input = latents_noisy[None, :, ...].repeat(2, 1, 1, 1, 1).reshape(-1, 4, resolution[0] // 8, resolution[1] // 8, )
            tt = t.reshape(1, 1).repeat(latent_model_input.shape[0], 1).reshape(-1)

            latent_model_input = self.scheduler.scale_model_input(latent_model_input, tt[0])
            if use_control_net:
                pred_depth_input = pred_depth_input[None, :, ...].repeat(1 + K, 1, 3, 1, 1).reshape(-1, 3, 512, 512).half()
                down_block_res_samples, mid_block_res_sample = self.controlnet_depth(
                    latent_model_input,
                    tt,
                    encoder_hidden_states=text_embeddings,
                    controlnet_cond=pred_depth_input,
                    return_dict=False,
                )
                unet_output = self.unet(latent_model_input, tt, encoder_hidden_states=text_embeddings,
                                    down_block_additional_residuals=down_block_res_samples,
                                    mid_block_additional_residual=mid_block_res_sample).sample
            else:
                unet_output = self.unet(latent_model_input.to(self.precision_t), tt.to(self.precision_t), encoder_hidden_states=text_embeddings_0.to(self.precision_t), added_cond_kwargs={"text_embeds": text_embeddings.reshape(-1, text_embeddings.shape[-1]), "time_ids": self.add_time_ids}).sample

            unet_output = unet_output.reshape(2, -1, 4, resolution[0] // 8, resolution[1] // 8, )
            noise_pred_uncond, noise_pred_text = unet_output[:1].reshape(-1, 4, resolution[0] // 8, resolution[1] // 8, ), unet_output[1:].reshape(-1, 4, resolution[0] // 8, resolution[1] // 8, )
            delta_DSD = noise_pred_text - noise_pred_uncond
        
        pred_noise = noise_pred_uncond + guidance_opt.guidance_scale * delta_DSD

        w = lambda alphas: (((1 - alphas) / alphas) ** 0.5)

        grad_mu2t = w(self.alphas[t]) * (pred_noise - target_mu2t)

        grad = torch.nan_to_num(grad_scale * grad_mu2t)

        loss = SpecifyGradient.apply(latents, grad)
              
        if iteration % guidance_opt.vis_interval == 0:
            noise_pred_post = noise_pred_uncond + 7.5* delta_DSD    
            lat2rgb = lambda x: torch.clip((x.permute(0,2,3,1) @ self.rgb_latent_factors.to(x.dtype)).permute(0,3,1,2), 0., 1.)
            save_path_iter = os.path.join(save_folder,"iter_{}_step_{}.jpg".format(iteration,prev_t.item()))
            with torch.no_grad():
                pred_x0_latent_sp = pred_original(self.scheduler, noise_pred_uncond, prev_t, prev_latents_noisy)    
                pred_x0_latent_pos = pred_original(self.scheduler, noise_pred_post, prev_t, prev_latents_noisy)        
                pred_x0_pos = self.decode_latents(pred_x0_latent_pos.type(self.precision_t))
                pred_x0_sp = self.decode_latents(pred_x0_latent_sp.type(self.precision_t))
                # pred_x0_uncond = pred_x0_sp[:1, ...]

                grad_abs = torch.abs(grad.detach())
                norm_grad  = F.interpolate((grad_abs / grad_abs.max()).mean(dim=1,keepdim=True), (resolution[0], resolution[1]), mode='bilinear', align_corners=False).repeat(1,3,1,1)

                latents_rgb = F.interpolate(lat2rgb(latents), (resolution[0], resolution[1]), mode='bilinear', align_corners=False)
                latents_sp_rgb = F.interpolate(lat2rgb(pred_x0_latent_sp), (resolution[0], resolution[1]), mode='bilinear', align_corners=False)

                viz_images = torch.cat([pred_rgb, 
                                        pred_depth.repeat(1, 3, 1, 1), 
                                        pred_alpha.repeat(1, 3, 1, 1), 
                                        rgb2sat(pred_rgb, pred_alpha).repeat(1, 3, 1, 1),
                                        latents_rgb, latents_sp_rgb, norm_grad,
                                        pred_x0_sp, pred_x0_pos],dim=0) 
                save_image(viz_images, save_path_iter)

        return loss

    def decode_latents(self, latents):
        target_dtype = latents.dtype
        latents = latents / self.vae.config.scaling_factor

        imgs = self.vae.decode(latents.to(self.vae.dtype)).sample
        imgs = (imgs / 2 + 0.5).clamp(0, 1)

        return imgs.to(target_dtype)

    def encode_imgs(self, imgs):
        target_dtype = imgs.dtype
        # imgs: [B, 3, H, W]
        imgs = 2 * imgs - 1

        posterior = self.vae.encode(imgs.to(self.vae.dtype)).latent_dist
        kl_divergence = posterior.kl()

        latents = posterior.sample() * self.vae.config.scaling_factor

        return latents.to(target_dtype), kl_divergence