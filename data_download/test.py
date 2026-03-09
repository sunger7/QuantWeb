import pandas as pd
import os
from pathlib import Path

# 上证日线数据目录
data_dir = '/Users/winssion/Desktop/akshare_proj/data/上证日线'

# 遍历目录中的所有CSV文件
csv_files = [f for f in os.listdir(data_dir) if f.endswith('.csv')]
print(f"找到 {len(csv_files)} 个CSV文件")

processed_count = 0
for csv_file in csv_files:
    file_path = os.path.join(data_dir, csv_file)
    
    try:
        # 读取CSV文件
        df = pd.read_csv(file_path)
        
        # 将日期列转换为datetime格式
        df['日期'] = pd.to_datetime(df['日期'])
        
        # 筛选出2023年及以后的数据
        df_filtered = df[df['日期'] >= '2023-01-01'].copy()
        
        # 删除重复日期（保留第一条）
        df_filtered = df_filtered.drop_duplicates(subset=['日期'], keep='first')
        
        # 按日期排序
        df_filtered = df_filtered.sort_values('日期').reset_index(drop=True)
        
        # 转换日期列为字符串格式用于保存
        df_filtered['日期'] = df_filtered['日期'].dt.strftime('%Y-%m-%d')
        
        # 保存处理后的数据
        df_filtered.to_csv(file_path, index=False)
        
        processed_count += 1
        
        # 打印处理结果
        original_rows = len(df)
        filtered_rows = len(df_filtered)
        removed_rows = original_rows - filtered_rows
        print(f"✓ {csv_file}: 原始行数={original_rows}, 处理后={filtered_rows}, 删除={removed_rows}")
        
    except Exception as e:
        print(f"✗ {csv_file}: 处理失败 - {str(e)}")

print(f"\n处理完成！已处理 {processed_count} 个文件")