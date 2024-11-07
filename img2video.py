import cv2
import os
from tqdm import tqdm

def images_to_video(image_folder, output_path, fps=30):
    """
    将文件夹中的图片序列转换为视频
    
    Args:
        image_folder: 包含图片序列的文件夹路径
        output_path: 输出视频的路径
        fps: 视频帧率，默认30
    """
    # 获取所有png图片并排序
    images = [img for img in os.listdir(image_folder) if img.endswith(".png")]
    images.sort()  # 确保按正确顺序读取图片
    
    if not images:
        print("No images found!")
        return
    
    # 读取第一张图片获取尺寸
    frame = cv2.imread(os.path.join(image_folder, images[0]))
    height, width, _ = frame.shape
    
    # 创建视频写入器
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # 或使用 'avc1'
    video_writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
    
    # 逐帧写入
    for image in tqdm(images, desc="Converting to video"):
        frame = cv2.imread(os.path.join(image_folder, image))
        video_writer.write(frame)
    
    video_writer.release()
    print(f"Video saved to {output_path}")

image_folder = "./output/67ae4d09-e/video/ours_30000" 
output_path = "output_video/bicycle.mp4"
images_to_video(image_folder, output_path, fps=20)
