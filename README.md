# Dreamer XL: Towards High-Resolution Text-to-3D Generation via Trajectory Score Matching

[Paper PDF (Arxiv)](https://arxiv.org/abs/2405.11252)

# Setup

## Cloning the Repository

```shell
git clone https://github.com/xingy038/Dreamer-XL.git --recursive
```

## Environment
Our default, provided install method is based on Conda package.
Firstly, you need to create an virtual environment and install the submodoules we provide. (slightly difference from original [3DGS](https://github.com/graphdeco-inria/gaussian-splatting))
```shell
conda create -n DreamerXL python=3.9.16 cudatoolkit=11.8
conda activate DreamerXL
pip install -r requirements.txt
pip install submodules/diff-gaussian-rasterization/
pip install submodules/simple-knn/
```

# Running
We will provide four templates config for training. (all configs can be trained in a single A100).

The pre-trained model will be downloaded automatically. You can also change ```model_key:``` in the ```configs/<config_file>.yaml``` to link the local Pretrained Diffusion Models ( [Stable Diffusion XL 1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/tree/main) in default)

## Some tips:

If you using vanilla Stable Diffusion XL 0.9 or 1.0, please make sure to use ```torch.float32``` in ```guidance/sd_step.py``` line 81. If you would like to use ```torch.float16```, you can try ```madebyollin/sdxl-vae-fp16-fix```, but this may result in NaN loss. Additionally, the generation results of avatars by vanilla Stable Diffusion XL 0.9 or 1.0 are not satisfactory, so we recommend trying [Civitai](https://civitai.com/). The model we tested well is [ZavyChromaXL](https://civitai.com/models/119229/zavychromaxl). The generated results are highly dependent on the initial results. Once your generated results are unreasonable, please check your initialization results. By the way, the initialization results of A100 are not particularly good.

```shell
python train.py --opt <path to config file>
```

```shell
bagel.yaml
batman.yaml
dog.yaml
Iron_Man.yaml
```


```latex
@misc{miao2024dreamer,
      title={Dreamer XL: Towards High-Resolution Text-to-3D Generation via Trajectory Score Matching}, 
      author={Xingyu Miao and Haoran Duan and Varun Ojha and Jun Song and Tejal Shah and Yang Long and Rajiv Ranjan},
      year={2024},
      eprint={2405.11252},
      archivePrefix={arXiv},
      primaryClass={cs.CV}
}
```
