import pandas as pd
import numpy as np

# ===================== 路径配置 =====================
INPUT_CSV  = "prediction_target_test_2.csv"  # 你的结构参数文件
OUTPUT_CSV = "Prediciton_FEM_test_150w.csv"     # 输出完整桁架数据

# ===================== 8个固定顶点 =====================
fixed_vertices = [
    [0.0, 0.0, 0.0],
    [1.0, 0.0, 0.0],
    [1.0, 1.0, 0.0],
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
    [1.0, 0.0, 1.0],
    [1.0, 1.0, 1.0],
    [0.0, 1.0, 1.0]
]

# ===================== 固定连接关系 =====================
connectivity = [
    [8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 6], [8, 7],
    [9, 0], [9, 1], [9, 2], [9, 3],
    [10, 4], [10, 5], [10, 6], [10, 7],
    [11, 0], [11, 1], [11, 5], [11, 4],
    [12, 2], [12, 3], [12, 7], [12, 6],
    [13, 0], [13, 3], [13, 7], [13, 4],
    [14, 1], [14, 2], [14, 6], [14, 5]
]

# ===================== 读取结构参数 =====================
df = pd.read_csv(INPUT_CSV)
print(f"✅ 读取到 {len(df)} 组结构")

# ===================== 逐行重建 nodes =====================
output_rows = []

for idx, row in df.iterrows():
    # 1. 读取相对密度
    rel_density = row["relative_density"]

    # 2. 读取所有点（你现在的格式）
    p1 = [row["point1_x"], row["point1_y"], row["point1_z"]]
    p2 = [row["point2_x"], row["point2_y"], 0.0]
    p3 = [row["point2_x"], row["point2_y"], 1.0]
    p4 = [row["point4_x"], 0.0, row["point4_z"]]
    p5 = [row["point4_x"], 1.0, row["point4_z"]]
    p6 = [0.0, row["point6_y"], row["point6_z"]]
    p7 = [1.0, row["point6_y"], row["point6_z"]]

    # 3. 拼接完整 15 个节点
    full_nodes = fixed_vertices + [p1, p2, p3, p4, p5, p6, p7]
    nodes_str = str(full_nodes)
    conn_str  = str(connectivity)

    # ===================== ✅ 【新增】读取9个刚度值 =====================
    C11 = row["C11"]
    C12 = row["C12"]
    C13 = row["C13"]
    C22 = row["C22"]
    C23 = row["C23"]
    C33 = row["C33"]
    C44 = row["C44"]
    C55 = row["C55"]
    C66 = row["C66"]

    # 5. 加入结果（包含刚度）
    output_rows.append([
        rel_density,
        nodes_str,
        conn_str,
        C11, C12, C13,
        C22, C23, C33,
        C44, C55, C66
    ])

# ===================== 保存最终 CSV =====================
result_df = pd.DataFrame(output_rows, columns=[
    "relative_density",
    "nodes",
    "connectivity",
    "C11","C12","C13",
    "C22","C23","C33",
    "C44","C55","C66"
])

result_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

print("\n🎉 全部重建完成！")
print(f"✅ 输出文件：{OUTPUT_CSV}")
print(f"✅ 格式：相对密度 + 完整节点 + 连接关系 + 9个刚度")
print("✅ 可直接用于有限元计算！")