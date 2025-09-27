# compare_emissions.py - 网约车碳排放对比可视化

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import os
import sys
from matplotlib import rcParams

# 设置中文字体
rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans']
rcParams['axes.unicode_minus'] = False


class EmissionComparator:
    """网约车碳排放对比分析器"""

    def __init__(self):
        self.hv_data = None
        self.av_data = None

    def load_emission_data(self, hv_dir, av_dir):
        """加载HV和AV模式的排放数据"""
        try:
            # 加载HV数据
            hv_file = os.path.join(hv_dir, "emission_details.csv")
            if os.path.exists(hv_file):
                self.hv_data = pd.read_csv(hv_file, encoding='utf-8')
                print(f"✓ 加载HV数据: {len(self.hv_data)} 条记录")
            else:
                print(f"✗ HV数据文件不存在: {hv_file}")
                return False

            # 加载AV数据
            av_file = os.path.join(av_dir, "emission_details.csv")
            if os.path.exists(av_file):
                self.av_data = pd.read_csv(av_file, encoding='utf-8')
                print(f"✓ 加载AV数据: {len(self.av_data)} 条记录")
            else:
                print(f"✗ AV数据文件不存在: {av_file}")
                return False

            return True

        except Exception as e:
            print(f"✗ 数据加载失败: {e}")
            return False

    def filter_ridehail_data(self):
        """筛选网约车数据"""
        if self.hv_data is not None:
            # 筛选网约车数据（vehicle_type为ridehail_car）
            self.hv_ridehail = self.hv_data[
                self.hv_data['vehicle_type'] == 'ridehail_car'
                ].copy()
            print(f"HV网约车记录: {len(self.hv_ridehail)} 条")

        if self.av_data is not None:
            self.av_ridehail = self.av_data[
                self.av_data['vehicle_type'] == 'ridehail_car'
                ].copy()
            print(f"AV网约车记录: {len(self.av_ridehail)} 条")

    def aggregate_by_time(self, time_interval=300):
        """按时间间隔聚合排放数据"""

        def aggregate_data(data, label):
            if data is None or len(data) == 0:
                return pd.DataFrame()

            # 创建时间窗口
            data['time_window'] = (data['timestamp'] // time_interval) * time_interval

            # 按时间窗口聚合
            agg_data = data.groupby('time_window').agg({
                'co2_equivalent_kg': 'sum',
                'co2_g': 'sum',
                'vehicle_id': 'nunique',  # 独特车辆数
                'speed_kmh': 'mean',
                'is_parking': 'sum'
            }).reset_index()

            agg_data['mode'] = label
            agg_data['time_minutes'] = agg_data['time_window'] / 60

            return agg_data

        self.hv_agg = aggregate_data(self.hv_ridehail, 'HV模式')
        self.av_agg = aggregate_data(self.av_ridehail, 'AV模式')

        print(f"HV聚合数据: {len(self.hv_agg)} 个时间窗口")
        print(f"AV聚合数据: {len(self.av_agg)} 个时间窗口")

    def create_comparison_plots(self, output_dir="comparison_plots"):
        """创建对比图表"""
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # 图1: 累积碳排放对比
        self.plot_cumulative_emissions(output_dir)

        # 图2: 分时段排放对比
        self.plot_time_series_emissions(output_dir)

        # 图3: 排放统计对比
        self.plot_emission_statistics(output_dir)

        # 图4: 速度vs排放关系
        self.plot_speed_emission_relationship(output_dir)

        print(f"\n所有图表已保存到: {output_dir}/")

    def plot_cumulative_emissions(self, output_dir):
        """累积碳排放对比图"""
        plt.figure(figsize=(12, 8))

        if len(self.hv_agg) > 0:
            hv_cumsum = self.hv_agg['co2_equivalent_kg'].cumsum()
            plt.plot(self.hv_agg['time_minutes'], hv_cumsum,
                     'b-', linewidth=2, label=f'HV模式 (总计: {hv_cumsum.iloc[-1]:.3f} kg)')

        if len(self.av_agg) > 0:
            av_cumsum = self.av_agg['co2_equivalent_kg'].cumsum()
            plt.plot(self.av_agg['time_minutes'], av_cumsum,
                     'r-', linewidth=2, label=f'AV模式 (总计: {av_cumsum.iloc[-1]:.3f} kg)')

            # 计算减排效果
            if len(self.hv_agg) > 0:
                reduction = ((hv_cumsum.iloc[-1] - av_cumsum.iloc[-1]) / hv_cumsum.iloc[-1]) * 100
                plt.title(f'网约车累积CO₂当量排放对比\n减排效果: {reduction:.1f}%', fontsize=14)

        plt.xlabel('仿真时间 (分钟)', fontsize=12)
        plt.ylabel('累积CO₂当量排放 (kg)', fontsize=12)
        plt.legend(fontsize=11)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f'{output_dir}/cumulative_emissions.png', dpi=300, bbox_inches='tight')
        plt.show()

    def plot_time_series_emissions(self, output_dir):
        """分时段排放对比图"""
        plt.figure(figsize=(14, 8))

        if len(self.hv_agg) > 0:
            plt.bar(self.hv_agg['time_minutes'] - 1, self.hv_agg['co2_equivalent_kg'],
                    width=2, alpha=0.7, label='HV模式', color='blue')

        if len(self.av_agg) > 0:
            plt.bar(self.av_agg['time_minutes'] + 1, self.av_agg['co2_equivalent_kg'],
                    width=2, alpha=0.7, label='AV模式', color='red')

        plt.xlabel('仿真时间 (分钟)', fontsize=12)
        plt.ylabel('时段CO₂当量排放 (kg)', fontsize=12)
        plt.title('网约车分时段排放对比', fontsize=14)
        plt.legend(fontsize=11)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f'{output_dir}/time_series_emissions.png', dpi=300, bbox_inches='tight')
        plt.show()

    def plot_emission_statistics(self, output_dir):
        """排放统计对比图"""
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 10))

        # 总排放对比
        if len(self.hv_agg) > 0 and len(self.av_agg) > 0:
            total_hv = self.hv_agg['co2_equivalent_kg'].sum()
            total_av = self.av_agg['co2_equivalent_kg'].sum()

            ax1.bar(['HV模式', 'AV模式'], [total_hv, total_av],
                    color=['blue', 'red'], alpha=0.7)
            ax1.set_ylabel('总CO₂当量排放 (kg)')
            ax1.set_title('总排放对比')

            # 添加减排百分比
            reduction = ((total_hv - total_av) / total_hv) * 100
            ax1.text(1, total_av + 0.1, f'-{reduction:.1f}%',
                     ha='center', fontweight='bold', color='green')

        # 平均速度对比
        if len(self.hv_agg) > 0 and len(self.av_agg) > 0:
            avg_speed_hv = self.hv_agg['speed_kmh'].mean()
            avg_speed_av = self.av_agg['speed_kmh'].mean()

            ax2.bar(['HV模式', 'AV模式'], [avg_speed_hv, avg_speed_av],
                    color=['blue', 'red'], alpha=0.7)
            ax2.set_ylabel('平均速度 (km/h)')
            ax2.set_title('平均速度对比')

        # 车辆数对比
        if len(self.hv_agg) > 0 and len(self.av_agg) > 0:
            vehicles_hv = self.hv_agg['vehicle_id'].mean()
            vehicles_av = self.av_agg['vehicle_id'].mean()

            ax3.bar(['HV模式', 'AV模式'], [vehicles_hv, vehicles_av],
                    color=['blue', 'red'], alpha=0.7)
            ax3.set_ylabel('平均活跃车辆数')
            ax3.set_title('活跃车辆数对比')

        # 停车次数对比
        if len(self.hv_agg) > 0 and len(self.av_agg) > 0:
            parking_hv = self.hv_agg['is_parking'].sum()
            parking_av = self.av_agg['is_parking'].sum()

            ax4.bar(['HV模式', 'AV模式'], [parking_hv, parking_av],
                    color=['blue', 'red'], alpha=0.7)
            ax4.set_ylabel('停车事件总数')
            ax4.set_title('停车行为对比')

        plt.tight_layout()
        plt.savefig(f'{output_dir}/emission_statistics.png', dpi=300, bbox_inches='tight')
        plt.show()

    def plot_speed_emission_relationship(self, output_dir):
        """速度与排放关系图"""
        plt.figure(figsize=(12, 8))

        if len(self.hv_ridehail) > 0:
            # HV模式散点图
            plt.scatter(self.hv_ridehail['speed_kmh'], self.hv_ridehail['co2_equivalent_kg'],
                        alpha=0.3, s=10, label='HV模式', color='blue')

        if len(self.av_ridehail) > 0:
            # AV模式散点图
            plt.scatter(self.av_ridehail['speed_kmh'], self.av_ridehail['co2_equivalent_kg'],
                        alpha=0.3, s=10, label='AV模式', color='red')

        plt.xlabel('速度 (km/h)', fontsize=12)
        plt.ylabel('瞬时CO₂当量排放 (kg)', fontsize=12)
        plt.title('速度与排放关系', fontsize=14)
        plt.legend(fontsize=11)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f'{output_dir}/speed_emission_relationship.png', dpi=300, bbox_inches='tight')
        plt.show()

    def print_summary(self):
        """打印对比总结"""
        print("\n" + "=" * 60)
        print("网约车碳排放对比总结")
        print("=" * 60)

        if len(self.hv_agg) > 0 and len(self.av_agg) > 0:
            hv_total = self.hv_agg['co2_equivalent_kg'].sum()
            av_total = self.av_agg['co2_equivalent_kg'].sum()
            reduction = ((hv_total - av_total) / hv_total) * 100

            print(f"HV模式总排放: {hv_total:.3f} kg CO₂当量")
            print(f"AV模式总排放: {av_total:.3f} kg CO₂当量")
            print(f"减排量: {hv_total - av_total:.3f} kg ({reduction:.1f}%)")

            # 平均速度对比
            hv_speed = self.hv_agg['speed_kmh'].mean()
            av_speed = self.av_agg['speed_kmh'].mean()
            print(f"\n平均速度:")
            print(f"HV模式: {hv_speed:.1f} km/h")
            print(f"AV模式: {av_speed:.1f} km/h")
            print(f"速度变化: {av_speed - hv_speed:+.1f} km/h")

            # 车辆活跃度
            hv_vehicles = self.hv_agg['vehicle_id'].mean()
            av_vehicles = self.av_agg['vehicle_id'].mean()
            print(f"\n平均活跃车辆:")
            print(f"HV模式: {hv_vehicles:.1f} 辆")
            print(f"AV模式: {av_vehicles:.1f} 辆")

        print("=" * 60)


def main():
    """主函数"""
    print("网约车碳排放对比分析工具")
    print("=" * 40)

    # 输入数据目录
    print("\n请输入数据目录路径:")
    hv_dir = input("HV模式结果目录 (默认: wudaokou_roadside_parking): ").strip()
    if not hv_dir:
        hv_dir = "wudaokou_roadside_parking"

    av_dir = input("AV模式结果目录 (默认: wudaokou_roadside_parking_av): ").strip()
    if not av_dir:
        av_dir = "wudaokou_roadside_parking_av"

    # 初始化对比器
    comparator = EmissionComparator()

    # 加载数据
    if not comparator.load_emission_data(hv_dir, av_dir):
        print("数据加载失败，退出程序")
        return

    # 筛选网约车数据
    comparator.filter_ridehail_data()

    # 按时间聚合
    time_interval = 300  # 5分钟间隔
    comparator.aggregate_by_time(time_interval)

    # 创建对比图表
    comparator.create_comparison_plots()

    # 打印总结
    comparator.print_summary()


if __name__ == "__main__":
    main()