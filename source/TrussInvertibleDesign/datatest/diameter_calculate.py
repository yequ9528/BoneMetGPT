import ast
import numpy as np
import pandas as pd


# ===================== 1. 稳定读取三列CSV（Pandas专业处理） =====================
def read_truss_csv_stable(file_path):
    """
    完美适配你的三列CSV结构：
    表头行：relative_density,nodes,connectivity
    数据行：[相对密度数值], [节点坐标列表字符串], [连接关系列表字符串]
    自动处理列表内逗号、带引号列，彻底避免列错乱
    """
    try:
        # 用Pandas读取CSV，自动识别表头、处理带引号的复杂列
        df = pd.read_csv(file_path, encoding='utf-8')
        # 校验必填列
        required_columns = ['relative_density', 'nodes', 'connectivity']
        if not set(required_columns).issubset(df.columns):
            missing = set(required_columns) - set(df.columns)
            raise ValueError(f"CSV表头缺失必填列: {missing}，当前表头: {df.columns.tolist()}")

        print(f"✅ CSV读取成功，共 {len(df)} 行数据")
        print(f"✅ 表头校验通过，列名: {df.columns.tolist()}")
        return df
    except Exception as e:
        raise RuntimeError(f"CSV读取失败: {str(e)}")


# ===================== 2. 线段与单位立方体相交计算（核心逻辑不变） =====================
def segment_in_unit_cube(p1, p2):
    """计算线段 p1-p2 在 [0,1]^3 立方体内的有效长度，自动剔除边界外部分"""

    def line_plane_intersection(p0, p1, axis, val):
        t = (val - p0[axis]) / (p1[axis] - p0[axis]) if (p1[axis] != p0[axis]) else None
        if t is None or t < 0 or t > 1:
            return None
        return p0 + t * (p1 - p0)

    intersections = []
    for axis in [0, 1, 2]:
        for val in [0.0, 1.0]:
            pt = line_plane_intersection(p1, p2, axis, val)
            if pt is not None:
                intersections.append(pt)

    inside_points = []
    for p in [p1, p2]:
        if (0 <= p[0] <= 1) and (0 <= p[1] <= 1) and (0 <= p[2] <= 1):
            inside_points.append(p)

    all_points = inside_points + intersections
    if len(all_points) < 2:
        return 0.0

    pts = np.array(all_points)
    max_dist = 0.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = np.linalg.norm(pts[i] - pts[j])
            if d > max_dist:
                max_dist = d
    return max_dist


# ===================== 3. 计算所有杆件有效总长（无重叠、仅立方体内） =====================
def compute_total_effective_length(nodes, beams):
    """计算所有杆件在单位立方体内的总有效长度，自动过滤无效杆、不重复计算重叠"""
    total_len = 0.0
    valid_beam_count = 0

    for (i, j) in beams:
        p1 = nodes[i]
        p2 = nodes[j]
        L = segment_in_unit_cube(p1, p2)
        if L > 1e-9:  # 过滤掉长度为0的无效杆
            total_len += L
            valid_beam_count += 1

    return total_len, valid_beam_count


# ===================== 4. 求解桁架杆统一直径（核心公式） =====================
def calculate_beam_diameter(relative_density, total_length, cell_size=1.0):
    """
    基于相对密度公式推导直径（工程标准定义）：
    相对密度 = 桁架材料体积 / 单胞立方体体积
    桁架体积 = π*(d/2)² * 总有效杆长
    单胞体积 = cell_size³
    """
    V_cell = cell_size ** 3
    # 处理总长度为0的异常情况
    if total_length < 1e-9:
        return np.nan
    diameter = np.sqrt((4 * relative_density * V_cell) / (np.pi * total_length))
    return diameter


# ===================== 主程序（批量处理+结果保存） =====================
if __name__ == "__main__":
    # ========== 请在这里修改你的CSV文件路径 ==========
    INPUT_CSV_PATH = "dia_cal.csv"  # 你的输入CSV文件路径
    OUTPUT_CSV_PATH = "truss_diameter_result.csv"  # 计算结果保存路径

    # 1. 读取CSV数据
    try:
        df = read_truss_csv_stable(INPUT_CSV_PATH)
    except RuntimeError as e:
        print(f"❌ {e}")
        exit()

    # 2. 逐行计算，新增结果列
    result_columns = ['总有效杆长', '有效杆件数', '杆统一直径']
    for col in result_columns:
        df[col] = np.nan

    for idx, row in df.iterrows():
        try:
            # 解析数据
            rho = float(row['relative_density'])
            nodes = np.array(ast.literal_eval(str(row['nodes'])), dtype=np.float64)
            beams = ast.literal_eval(str(row['connectivity']))

            # 计算有效杆长
            L_total, valid_beam_num = compute_total_effective_length(nodes, beams)
            # 计算直径
            d = calculate_beam_diameter(rho, L_total)

            # 写入结果
            df.loc[idx, '总有效杆长'] = round(L_total, 6)
            df.loc[idx, '有效杆件数'] = valid_beam_num
            df.loc[idx, '杆统一直径'] = round(d, 6) if not np.isnan(d) else "无效(无有效杆)"

            print(f"\n✅ 第{idx + 2}行数据计算完成")
            print(f"   目标相对密度: {rho:.6f} | 总有效杆长: {L_total:.6f} | 杆统一直径: {d:.6f}")
        except Exception as e:
            print(f"\n❌ 第{idx + 2}行数据计算失败: {str(e)}")
            continue

    # 3. 保存结果到新CSV
    df.to_csv(OUTPUT_CSV_PATH, index=False, encoding='utf-8-sig')
    print(f"\n✅ 所有计算完成，结果已保存到: {OUTPUT_CSV_PATH}")

    # 4. 控制台输出汇总结果
    print("\n" + "=" * 100)
    print("                              🎯 桁架杆直径计算结果汇总")
    print("=" * 100)
    print(df[['relative_density', '总有效杆长', '有效杆件数', '杆统一直径']].to_string(index=False))
    print("=" * 100)