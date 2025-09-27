# src/main.py

import os
import sys
import time
import signal
import traci
import csv
import numpy as np
import random
from config import SimulationConfig
from paths import PathManager
from traci_manager import TraCIManager
from parking_manager import ImprovedParkingDataManager
from detector import DynamicParkingDetector
from ridehail_coordinator import RidehailCoordinator
from utils import run_command
from simulation_setup import (
    download_osm_data,
    extract_road_info_from_network,
    create_routes_with_vehicle_types,
    create_parking_facilities,
    create_sumo_config,
    create_gui_settings,
    test_generated_files
)
from terminal_parking import TerminalParkingManager
from fleet_optimizer import VehicleShareabilityOptimizer


def main():
    """主程序"""
    print("=" * 80)
    print("  改进版北京道路停车综合仿真系统（含碳排放计算）")
    print("  Improved Beijing Road Parking Simulation with Emission Calculation")
    print("=" * 80)

    print("\n 选择仿真类型:")
    print("1. 局部仿真（五道口地区，快速测试）")
    print("2. 北京全域仿真（基于道路类型估算停车密度）")
    print("3. 自定义区域仿真")

    choice = input("请选择 (1/2/3): ").strip()

    if choice == '1':
        config = SimulationConfig("local")
        use_estimated_data = False
    elif choice == '2':
        config = SimulationConfig("beijing")
        use_estimated_data = True
    elif choice == '3':
        config = SimulationConfig("local")
        print("\n 自定义区域配置:")
        config.area_name = input("区域名称 (默认: custom): ").strip() or "custom"
        try:
            lat = float(input("中心纬度 (默认: 39.993): ") or "39.993")
            lon = float(input("中心经度 (默认: 116.336): ") or "116.336")
            config.center_coord = (lat, lon)
            radius = int(input("半径(米) (默认: 2000): ") or "2000")
            config.radius_meters = radius
            config.output_dir_name = f"{config.area_name}_roadside_parking"
        except ValueError:
            print(" 输入格式错误，使用默认值")
        use_estimated_data = input("是否使用基于道路类型的停车密度估算? (y/n): ").strip().lower() == 'y'
    else:
        print(" 无效选择")
        return

    # 统一设置随机种子
    random.seed(config.random_seed)
    np.random.seed(config.random_seed)

    paths = PathManager(config)
    traci_manager = TraCIManager()
    parking_manager = None
    total_private_cars_generated = 0  # 累计生成的私家车数
    total_private_cars_arrived = 0  # 累计到达目的地的私家车数
    private_car_ids_seen = set()  # 记录已经出现过的私家车ID

    print(f"\n工作目录: {paths.output_dir}")
    print(f"仿真类型: {config.simulation_type}")
    print(f"仿真区域: {config.area_name}")
    print(f"中心坐标: {config.center_coord}")
    print(f"半径: {config.radius_meters} 米")
    print(f"路边停车比例: {config.roadside_parking_ratio:.1%}")

    if use_estimated_data:
        print(f"\n--- 初始化停车密度估算 ---")
        parking_manager = ImprovedParkingDataManager(config)

    if not os.path.exists(paths.osm_file) or os.path.getsize(paths.osm_file) < 1024:
        if not download_osm_data(config, paths.osm_file): return
    else:
        print("✓ 使用已存在的OSM数据")

    if os.path.exists(paths.net_file) and os.path.getsize(paths.net_file) > 1024:
        print(f"✓ 使用已存在的路网文件: {paths.net_file}")
    else:
        cmd_netconvert = [
            paths.netconvert, "--osm-files", paths.osm_file, "-o", paths.net_file,
            "--geometry.remove", "--roundabouts.guess", "--ramps.guess", "--junctions.join",
            "--tls.guess-signals",
            "--ignore-errors", "true",
            "--remove-edges.isolated",
            "--keep-edges.by-vclass", "passenger",
            "--remove-edges.by-vclass", "hov,taxi,bus,delivery,truck,bicycle,pedestrian"
        ]
        # 修复 UnboundLocalError：将命令执行移入 else 块
        if not run_command(cmd_netconvert, "生成SUMO路网"): return

    road_edges = extract_road_info_from_network(paths.net_file)
    if not road_edges: return

    detector = DynamicParkingDetector(config, parking_manager)
    detector.debug_mode = False
    terminal_parking_manager = TerminalParkingManager(config)

    parking_info = create_parking_facilities(paths.net_file, config, paths)
    terminal_parking_manager.set_sumo_parking_areas(parking_info)

    if use_estimated_data and parking_manager:
        print(f"\n--- 生成停车密度估算 ---")
        parking_manager.generate_roadside_parking_density(road_edges)

    ridehail_coordinator = RidehailCoordinator(config)
    ridehail_coordinator.parking_detector = detector

    # 新增：加载预生成的网约车订单
    ridehail_orders_file = os.path.join(paths.output_dir, "ridehail_demands.csv")
    ridehail_coordinator.load_pregenerated_orders(ridehail_orders_file)

    for edge_id, edge_info in road_edges.items():
        detector.plan_parking_spots_for_edge(edge_id, edge_info['length'])

    if os.path.exists(paths.rou_file) and os.path.getsize(paths.rou_file) > 1024 and not config.force_rebuild_routes:
        print("✓ 使用已存在的路由文件")
    else:
        if not create_routes_with_vehicle_types(paths.net_file, paths.trips_file, paths.rou_file, config, paths):
            return
    if not create_sumo_config(paths.cfg_file, paths.net_file, paths.rou_file, config, paths): return
    create_gui_settings(os.path.join(paths.output_dir, 'gui.settings.xml'))
    if not test_generated_files(paths): return

    print("\n 文件准备完成!")
    if hasattr(config, 'fixed_ridehail_orders'):
        print(f"\n配置摘要:")
        print(f"  总需求: {config.total_trip_demand}")
        print(f"  私家车: {int(config.total_trip_demand * config.private_car_ratio)} (直接生成)")
        print(f"  网约车订单: {config.fixed_ridehail_orders}")
        print(f"  优化的网约车数: {config.optimal_fleet_size}")

    print(f"\n 选择运行模式:\n1. 无GUI模式（快速数据分析）\n2. GUI模式（可视化观察）\n3. 仅生成文件")
    run_choice = input("请选择 (1/2/3): ").strip()

    if run_choice == '3':
        print(" 文件已生成完毕！")
        return

    use_gui = run_choice == '2'
    sumo_cmd = [
        paths.sumo_gui if use_gui else paths.sumo, "-c", paths.cfg_file,
        "--step-length", "1", "--time-to-teleport", "-1", "--no-warnings",
        "--seed", str(config.random_seed),
        "--device.rerouting.probability", "1",  # 添加这行，为所有车辆启用路由设备
        "--device.rerouting.pre-period", "60"  # 添加这行，预计算路由
    ]
    if not traci_manager.start_connection(sumo_cmd): return

    data_source = "基于道路类型估算" if use_estimated_data and parking_manager else "默认配置"
    print(f"\n 仿真开始 ({data_source})...")
    print(" 正在计算碳排放...")
    print("私家车到达目的地后将寻找预设停车位")

    # 统计变量初始化
    total_private_cars_generated = 0
    total_private_cars_completed = 0
    private_cars_in_simulation = 0
    step_interval_vehicles = {}  # 存储各区间的车辆累计
    current_interval_start = 0
    current_interval_vehicles = 0

    # 替换原来的第190行
    total_private_cars_generated = 0
    if hasattr(config, 'vehicle_composition') and config.vehicle_composition:
        total_private_cars_generated = config.vehicle_composition.get('private_car', 0) * 2
    step = 0
    start_time = time.time()
    progress_interval = 200

    vehicles_at_destination = set()

    # === 修改点: 主循环条件 ===
    while step < config.simulation_duration:
        traci.simulationStep()

        detector.detect_events(step)
        ridehail_coordinator.step_update(step)

        current_vehicles = traci.vehicle.getIDList()

        # Fix 1: Wrap private car logic
        # 处理私家车到达目的地停车
        if config.private_car_ratio > 0:  # Only process if private cars exist
            for veh_id in current_vehicles:
                try:
                    if traci.vehicle.getTypeID(veh_id) != 'private_car':
                        continue
                except traci.TraCIException:
                    continue

                if veh_id not in vehicles_at_destination:
                    try:
                        route = traci.vehicle.getRoute(veh_id)
                        if not route: continue

                        if traci.vehicle.getRouteIndex(veh_id) == len(route) - 1:
                            destination_edge = route[-1]
                            terminal_parking_manager.find_and_assign_parking(veh_id, destination_edge)
                            vehicles_at_destination.add(veh_id)
                    except traci.TraCIException:
                        continue

            searching_now = list(terminal_parking_manager.searching_vehicles.keys())
            for veh_id in searching_now:
                try:
                    if veh_id in traci.vehicle.getIDList():
                        terminal_parking_manager.execute_parking_for_arrived_vehicle(veh_id)
                except traci.TraCIException:
                    continue

        # 累计区间车辆数
        if step > 0 and step <= progress_interval:
            current_interval_vehicles += len(current_vehicles)
        elif step > progress_interval and step % progress_interval == 1:
            # 保存上一个区间的数据
            interval_key = f"{current_interval_start}-{step - 1}"
            step_interval_vehicles[interval_key] = current_interval_vehicles
            # 开始新区间
            current_interval_start = step
            current_interval_vehicles = len(current_vehicles)
        elif step > progress_interval:
            current_interval_vehicles += len(current_vehicles)

        # 进度输出
        if step > 0 and step % progress_interval == 0:
            # 统计当前车辆
            current_vehicles = traci.vehicle.getIDList()

            # Fix 3: Update statistics collection
            if config.private_car_ratio > 0:
                private_cars_in_simulation = sum(1 for v in current_vehicles if 'private_car' in v)
            else:
                private_cars_in_simulation = 0

            ridehail_cars_in_simulation = sum(1 for v in current_vehicles if 'ridehail_car' in v)

            # 获取当前区间的累计
            current_interval_key = f"{step - progress_interval + 1}-{step}"
            if current_interval_key not in step_interval_vehicles:
                step_interval_vehicles[current_interval_key] = current_interval_vehicles

            roadside_summary = detector.get_summary()
            parking_lot_stats = terminal_parking_manager.get_parking_lot_statistics()

            elapsed = time.time() - start_time
            sps = step / elapsed if elapsed > 0 else 0
            progress = (step / config.simulation_duration) * 100

            print(f"\n [进度 {progress:.1f}%] 步长 {step}/{config.simulation_duration}")
            print(f"   速度: {sps:.1f} 步/秒 | 私家车: {private_cars_in_simulation} | 网约车: {ridehail_cars_in_simulation}")
            print(f"   当前停车: 路边 {roadside_summary.get('currently_roadside_parking', 0)} 辆 | "
                  f"停车场 {parking_lot_stats['occupied_parking_spots']} 辆 (寻找中: {parking_lot_stats['searching_vehicles']})")
            print(f"   累计: 路边临停 {roadside_summary.get('roadside_parking_starts', 0)} 次 | "
                  f"终点停车 {parking_lot_stats['parked_in_lots']} 辆")
            print(f"   CO2当量: {roadside_summary['total_co2_equivalent_kg']:.3f} kg")

            # 输出当前区间统计
            print(
                f"   [统计] {step - progress_interval + 1}-{step}步累计车辆总数: {step_interval_vehicles.get(current_interval_key, 0)}")

            # 获取网约车详细统计（包含等候时间）
            ridehail_stats = ridehail_coordinator.get_statistics()
            print(f"   网约车详情: 当前{ridehail_stats['current_fleet_size']}辆 | "
                  f"最优{ridehail_stats['optimal_fleet_size']}辆 | "
                  f"空闲{ridehail_stats['idle_vehicles']}辆 | "
                  f"忙碌{ridehail_stats['busy_vehicles']}辆")

            # 改进的订单统计输出
            recent_completion = ridehail_stats.get('recent_completion_rate', 0)
            recent_wait = ridehail_stats.get('recent_avg_wait_time', 0)
            overall_wait = ridehail_stats.get('overall_avg_wait_time', 0)

            print(f"   订单状态: 请求 {ridehail_stats['total_orders_requested']} | "
                  f"完成 {ridehail_stats['total_orders_completed']} | "
                  f"待处理 {ridehail_stats['pending_orders']} | "
                  f"近{progress_interval}步完成率: {recent_completion:.1f}%")
            print(f"   等候时间: 近{progress_interval}步平均 {recent_wait:.1f}步 | "
                  f"总体平均 {overall_wait:.1f}步")

            # 重置当前区间计数
            current_interval_vehicles = 0

        step += 1

    print("\n 仿真结束。")
    detector.save_results(paths)
    traci_manager.close_connection()

    print(f"\n 仿真完成！")


if __name__ == "__main__":
    def signal_handler(sig, frame):
        print(f"\n 收到中断信号，正在安全退出...")
        try:
            if traci.isLoaded():
                traci.close()
        except:
            pass
        sys.exit(0)


    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    main()