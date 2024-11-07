import torch
from scene import Scene
import os
from tqdm import tqdm
from os import makedirs
from gaussian_renderer import render
import torchvision
from utils.general_utils import safe_state
from argparse import ArgumentParser
from arguments import ModelParams, PipelineParams, get_combined_args
from gaussian_renderer import GaussianModel


def extract_watermark(dataset : ModelParams):
    with torch.no_grad():
        gaussians = GaussianModel(dataset)
        
        # 构建完整的模型路径
        model_path = os.path.join(dataset.model_path, "point_cloud", "iteration_30000", "point_cloud")
        if not os.path.exists(model_path + ".ply"):
            raise FileNotFoundError(f"Model file not found at {model_path}.ply")
            
        print(f"Loading model from: {model_path}")
        gaussians.load_model(model_path)

        gaussians.precompute(False)
        
        initial_watermark = ""
        watermarked_opacity = gaussians.extract_opacity()
        for scale, opacity in watermarked_opacity:
            print(f"\nScale {scale}:")
            print("Raw opacity values:")
            if opacity.numel() > 0: 
                decimal_part = str(f"{opacity[0].item():.8f}").split('.')[1]  # Get only the decimal part
                initial_watermark += decimal_part
            unique_opacities = set(f"{op.item():.8f}" for op in opacity)
            print("Unique opacity values:")
            for op in sorted(unique_opacities):
                print(op)
        print(f"\nNumber of different scales: {len(watermarked_opacity)}")
        print(f"Initial watermark: {initial_watermark}")
        compare_watermark(gaussians.watermark_value, initial_watermark)

def compare_watermark(s_init, s_extracted):
    matches = sum(c1 == c2 for c1, c2 in zip(s_init, s_extracted))
    print(f"matches: {matches}")
    similarity = matches / len(s_init)  # Normalize by length
    print(f"Similarity between strings: {similarity:.2%}")


if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Testing script parameters")
    model = ModelParams(parser, sentinel=True)
    pipeline = PipelineParams(parser)
    parser.add_argument("--iteration", default=-1, type=int)
    parser.add_argument("--skip_train", action="store_true")
    parser.add_argument("--skip_test", action="store_true")
    parser.add_argument("--quiet", action="store_true")

    args = get_combined_args(parser)
    print("Rendering " + args.model_path)
    
    extract_watermark(model.extract(args))
