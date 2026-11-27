import torch
import os

class MinMaxScaler:
    def __init__(self):
        self.min = None
        self.max = None

    def fit(self, x):
        self.min = torch.min(x, dim=0)[0]
        self.max = torch.max(x, dim=0)[0]
        self.max[self.max == self.min] = self.min[self.max == self.min] + 1e-8

    def normalize(self, x):
        return (x - self.min) / (self.max - self.min)

    def unnormalize(self, x_norm):
        return x_norm * (self.max - self.min) + self.min

    # ================= 【新增：保存归一化参数】 =================
    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        torch.save({
            'min': self.min,
            'max': self.max
        }, path)

    # ================= 【新增：加载归一化参数】 =================
    def load(self, path):
        data = torch.load(path, map_location='cpu', weights_only=True)
        if not isinstance(data, dict) or set(data) != {'min', 'max'}:
            raise ValueError('scaler payload must contain exactly min and max')
        if not torch.is_tensor(data['min']) or not torch.is_tensor(data['max']):
            raise ValueError('scaler min and max must be tensors')
        if data['min'].shape != data['max'].shape:
            raise ValueError('scaler min and max must have identical shapes')
        if not torch.isfinite(data['min']).all() or not torch.isfinite(data['max']).all():
            raise ValueError('scaler min and max must be finite')
        self.min = data['min']
        self.max = data['max']
