import torch

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

# training data path
dataPath = 'data/train_150w_data.csv'

# define column names from data
Point_names = ['relative_density','point1_x','point1_y','point1_z','point2_x','point2_y','point4_x','point4_z','point6_y','point6_z']
C_names = ['C11','C12','C13','C22','C23','C33','C44','C55','C66']

# define data type for correct scaling
point_types = ['continuous']*10
C_type = ['continuous']*9

# 训练参数
batchSize = 2048
numWorkers = 0
F1_train_epochs = 150
F1_learning_rate = 1e-3

# 网络结构 (输入10 → 输出9)
F1_arch = [512, 512, 256, 128]

F1_train = True

# ===================== 逆向模型mlp参数（新增！） =====================
inv_train_epochs = 150        # 逆向训练轮数
inv_learning_rate = 1e-3      # 逆向学习率
inv_noise_scale = 0.03        # 一对多生成噪声强度（可调）

# 逆向网络结构 (输入9 → 输出10)  刚度→结构
inv_arch = [512, 512, 256, 128]

inv_train = True              # 是否训练逆向模型