from plyfile import PlyData
 
# 替换为你的PLY文件路径
ply_file_path = '/home/hui_su/Compact-3DGS/output/a7d96ff0-f/point_cloud/iteration_30000/point_cloud.ply'
 
# 加载PLY文件
ply_data = PlyData.read(ply_file_path)
 
# 获取点云数量
num_points = len(ply_data['vertex'].data)
 
print(f'num_points: {num_points}')