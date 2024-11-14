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
    # 将图片调整为1080p分辨率
    frame = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_AREA)
    height, width, _ = frame.shape
    
    # 创建视频写入器
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # 或使用 'avc1'
    video_writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
    
    # 逐帧写入
    for image in tqdm(images, desc="Converting to video"):
        frame = cv2.imread(os.path.join(image_folder, image))
        # 将每一帧调整为1080p分辨率
        frame = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_AREA)
        video_writer.write(frame)

    video_writer.release()
    print(f"Video saved to {output_path}")

image_folder = "./output/b4d9dece-6/video/ours_30000" 
output_path = "output_video/truck_1.mp4"
images_to_video(image_folder, output_path, fps=20)


# import cv2

# def check_video_info(video_path):
#     # 打开视频文件
#     video = cv2.VideoCapture(video_path)
    
#     # 获取视频基本信息
#     fps = video.get(cv2.CAP_PROP_FPS)
#     frame_count = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
#     width = int(video.get(cv2.CAP_PROP_FRAME_WIDTH))
#     height = int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
    
#     # 获取视频编码格式
#     fourcc = int(video.get(cv2.CAP_PROP_FOURCC))
#     codec = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)])
    
#     print(f"视频路径: {video_path}")
#     print(f"分辨率: {width}x{height}")
#     print(f"帧率: {fps}")
#     print(f"总帧数: {frame_count}")
#     print(f"视频编码: {codec}")
    
#     # 释放视频对象
#     video.release()

# # 使用示例
# video_path = "output_video/bicycle.mp4"
# check_video_info(video_path)