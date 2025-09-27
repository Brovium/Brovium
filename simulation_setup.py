# src/simulation_setup.py

import os
import sys
import xml.etree.ElementTree as ET
import random
import shutil
from utils import run_command
from od_demand_generator import ODDemandGenerator
from fleet_optimizer import VehicleShareabilityOptimizer


class TripGenerator:
    """手动行程生成器"""

    def __init__(self, config):
        self.config = config

    def extract_edges_from_network(self, net_file):
        try:
            tree = ET.parse(net_file)
            root = tree.getroot()
            edges = []

            for edge in root.findall('edge'):
                if edge.get('function') != 'internal':
                    edge_info = {
                        'id': edge.get('id'),
                        'from': edge.get('from'),
                        'to': edge.get('to'),
                        'type': edge.get('type', ''),
                        'lanes': len(edge.findall('lane'))
                    }

                    lanes = edge.findall('lane')
                    if lanes:
                        edge_info['length'] = float(lanes[0].get('length', 0))
                    else:
                        edge_info['length'] = 0

                    edges.append(edge_info)

            return edges
        except Exception as e:
            print(f"✗ 提取边信息失败: {e}")
            return []

    def generate_trips_manually(self, net_file, trips_file):
        print("手动生成行程文件...")
        edges = self.extract_edges_from_network(net_file)
        if not edges:
            return False

        suitable_edges = [e for e in edges if e['length'] > 50 and not e['id'].startswith(':')]
        if len(suitable_edges) < 10:
            print("可用边数量不足")
            return False

        print(f"找到 {len(suitable_edges)} 条可用道路")

        trips_root = ET.Element('trips')
        base_trips = int(self.config.simulation_duration / self.config.trip_period)

        if self.config.simulation_type == "beijing":
            num_trips = base_trips * 5
        else:
            num_trips = base_trips

        print(f"生成 {num_trips} 个行程...")

        for i in range(num_trips):
            from_edge = random.choice(suitable_edges)
            to_edge = random.choice(suitable_edges)

            while to_edge['id'] == from_edge['id'] and len(suitable_edges) > 1:
                to_edge = random.choice(suitable_edges)

            if i < num_trips * 0.6:
                depart_time = random.uniform(0, self.config.simulation_duration * 0.33)
            else:
                depart_time = random.uniform(0, self.config.simulation_duration * 0.9)

            trip = ET.SubElement(trips_root, 'trip')
            trip.set('id', f'trip_{i}')
            trip.set('depart', f'{depart_time:.1f}')
            trip.set('from', from_edge['id'])
            trip.set('to', to_edge['id'])

        if sys.version_info >= (3, 9):
            ET.indent(trips_root, space="    ")

        ET.ElementTree(trips_root).write(trips_file, encoding='utf-8', xml_declaration=True)
        print(f"行程文件已生成")
        return True


def download_osm_data(config, output_file):
    """下载OSM数据 - 基于行政区划边界"""
    print(f"\n--- 步骤: 下载{config.area_name}OSM数据 ---")
    try:
        import requests
    except ImportError:
        print("需要安装requests库: pip install requests")
        return False

    if config.simulation_type == "beijing":
        # 使用四边形边界框查询北京全域
        min_lat = 39.880274
        max_lat = 40.763304
        min_lon = 113.243052
        max_lon = 117.6997

        query = f'''[out:xml][timeout:900];
        (
          way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|service|living_street)$"]({min_lat},{min_lon},{max_lat},{max_lon});
        );
        (._;>;);
        out meta;'''

        print("正在下载北京市完整路网...")
        print(f"使用边界框: [{min_lat},{min_lon},{max_lat},{max_lon}]")
    else:
        # 局部区域仍使用半径查询
        lat, lon = config.center_coord
        radius = config.radius_meters
        query = f'[out:xml][timeout:300];(way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|service)$"](around:{radius},{lat},{lon}););(._;>;);out meta;'
        print(f"正在查询 {config.area_name} 道路数据...")

    try:
        # 使用备用服务器列表
        servers = [
            "http://overpass-api.de/api/interpreter",
            "https://lz4.overpass-api.de/api/interpreter",
            "https://z.overpass-api.de/api/interpreter"
        ]

        response = None
        for server in servers:
            try:
                print(f"尝试服务器: {server}")
                response = requests.post(server, data=query, timeout=900)
                response.raise_for_status()

                # 检查返回数据大小
                data_size_mb = len(response.content) / (1024 * 1024)
                print(f"下载数据大小: {data_size_mb:.1f} MB")

                if data_size_mb > 0.5:
                    break
                else:
                    print("数据量太小，尝试下一个服务器")
                    response = None
            except Exception as e:
                print(f"服务器 {server} 失败: {e}")
                continue

        if not response or len(response.content) < 1000:
            print("标准查询失败，尝试简化查询")
            fallback_query = '''[out:xml][timeout:600];
            (
              way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|service)$"](bbox:39.4,115.4,40.9,117.5);
            );
            (._;>;);
            out meta;'''

            response = requests.post("http://overpass-api.de/api/interpreter", data=fallback_query, timeout=600)
            response.raise_for_status()

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(response.text)

        print(f"OSM数据已保存到 {output_file}")

        if "way" in response.text and "highway" in response.text:
            way_count = response.text.count('<way')
            node_count = response.text.count('<node')
            print(f"数据验证通过，包含 {way_count} 条道路, {node_count} 个节点")
            return True
        else:
            print("数据验证失败")
            return False

    except Exception as e:
        print(f"下载失败: {e}")
        return False


def extract_road_info_from_network(net_file):
    """从SUMO网络文件提取道路信息"""
    print("提取道路信息...")
    try:
        tree = ET.parse(net_file)
        root = tree.getroot()
        road_edges = {}

        for edge in root.findall('edge'):
            if edge.get('function') != 'internal':
                lanes = edge.findall('lane')
                if lanes:
                    road_edges[edge.get('id')] = {
                        'id': edge.get('id'),
                        'type': edge.get('type', ''),
                        'from_node': edge.get('from'),
                        'to_node': edge.get('to'),
                        'lanes': len(lanes),
                        'length': float(lanes[0].get('length', 0))
                    }

        print(f"提取到 {len(road_edges)} 条道路")
        return road_edges
    except Exception as e:
        print(f"道路信息提取失败: {e}")
        return {}


def create_routes_with_vehicle_types(net_file, trips_file, rou_file, config, paths):
    """基于配置生成车辆和路由 - 修复版本"""
    import os, sys, random
    import xml.etree.ElementTree as ET

    road_edges = extract_road_info_from_network(net_file)
    if not road_edges:
        return False

    total_demand = int(getattr(config, "total_trip_demand", 10000))
    time_horizon = int(getattr(config, "time_horizon", 3600))
    depart_profile = getattr(config, "departure_time_profile", "uniform")

    print(f"\n--- 车队规模优化 ---")
    print(f"开始车队规模优化，总trips: {total_demand}")
    print(f"车辆出行分担比例: 私家车 {config.private_car_ratio:.1%}, 网约车 {config.ridehail_order_ratio:.1%}")

    od_generator = ODDemandGenerator(config)
    od_generator.initialize_zones_from_network(road_edges)
    od_matrix = od_generator.generate_od_matrix(total_demand)
    all_demands = od_generator.generate_demand_from_od(od_matrix)

    def _distribute_departures(demands, horizon, profile):
        n = len(demands)
        if n == 0: return
        if profile == "uniform":
            times = sorted(random.uniform(0, horizon - 1e-6) for _ in range(n))
        else:
            weights = [float(w) for w in profile if float(w) > 0]
            if not weights: weights = [1.0]
            s = sum(weights)
            probs = [w / s for w in weights]
            B = len(probs)
            bw = horizon / B
            alloc = [int(round(p * n)) for p in probs]
            diff = n - sum(alloc)
            for i in range(abs(diff)): alloc[i % B] += (1 if diff > 0 else -1)
            times = []
            for b, k in enumerate(alloc):
                start, end = b * bw, (b + 1) * bw
                for _ in range(k): times.append(random.uniform(start, max(end, start + 1e-6)))
            times.sort()
        for d, t in zip(demands, times): d.departure_time = float(t)

    _distribute_departures(all_demands, time_horizon, depart_profile)

    private_demands = [d for d in all_demands if d.demand_type == 'private_car']
    ridehail_demands = [d for d in all_demands if d.demand_type == 'ride_hailing']

    optimizer = VehicleShareabilityOptimizer(config)
    avg_trip_sec = int(getattr(config, "avg_ridehail_trip_seconds", 600))
    for d in ridehail_demands:
        optimizer.add_trip(
            trip_id=d.demand_id, pickup_time=d.departure_time,
            dropoff_time=d.departure_time + avg_trip_sec,
            pickup_location=d.origin_edge, dropoff_location=d.destination_edge,
            trip_type='ride_hailing'
        )

    network = optimizer.build_shareability_network()
    n_nodes = getattr(network, "number_of_nodes", lambda: 0)()
    n_edges = getattr(network, "number_of_edges", lambda: 0)()
    print(f"构建车辆共享网络，节点数: {n_nodes}, 边数: {n_edges}")

    ridehail_cars_optimized = int(optimizer.solve_minimum_path_cover())
    print(f"计算得到最优车队规模: {ridehail_cars_optimized}")

    private_cars_needed = len(private_demands) // 2
    vehicle_composition = {
        "private_car": int(private_cars_needed),
        "ride_hailing": int(ridehail_cars_optimized),
    }
    total_optimized = private_cars_needed + ridehail_cars_optimized
    dict_print = {"privatecar": private_cars_needed, "ridehailing": ridehail_cars_optimized}
    print(f"车辆类型分配: {dict_print}")
    print(f"优化后车队规模: {total_optimized}")
    eff = (1 - total_optimized / total_demand) * 100 if total_demand > 0 else 0
    print(f"效率提升: {eff:.1f}%")

    config.optimal_fleet_size = ridehail_cars_optimized
    config.vehicle_composition = vehicle_composition
    config.fixed_ridehail_orders = len(ridehail_demands)

    # 创建临时的trips文件，但不指定type（让duarouter处理）
    temp_trips = os.path.join(paths.output_dir, "temp_trips.xml")
    trips_root = ET.Element('trips')

    # 首先添加vType定义
    vtype_private = ET.SubElement(trips_root, 'vType')
    vtype_private.set('id', 'private_car')
    vtype_private.set('accel', '2.6')
    vtype_private.set('decel', '4.5')
    vtype_private.set('sigma', '0.5')
    vtype_private.set('length', '5.0')
    vtype_private.set('maxSpeed', '16.67')
    vtype_private.set('guiShape', 'passenger')
    vtype_private.set('color', '0,100,200')

    vtype_ridehail = ET.SubElement(trips_root, 'vType')
    vtype_ridehail.set('id', 'ridehail_car')
    vtype_ridehail.set('accel', '2.6')
    vtype_ridehail.set('decel', '4.5')
    vtype_ridehail.set('sigma', '0.5')
    vtype_ridehail.set('length', '5.0')
    vtype_ridehail.set('maxSpeed', '16.67')
    vtype_ridehail.set('guiShape', 'passenger')
    vtype_ridehail.set('color', '255,128,0')

    # 然后添加trips
    for i in range(0, len(private_demands), 2):
        if i + 1 >= len(private_demands): break
        d_go = private_demands[i]
        if d_go.origin_edge and d_go.destination_edge:
            trip_go = ET.SubElement(trips_root, 'trip')
            trip_go.set('id', f"private_car_{i // 2}_go")
            trip_go.set('depart', f'{d_go.departure_time:.1f}')
            trip_go.set('from', d_go.origin_edge)
            trip_go.set('to', d_go.destination_edge)
            trip_go.set('type', 'private_car')

            return_time = d_go.departure_time + random.uniform(1800, 3600)
            if return_time < time_horizon:
                trip_ret = ET.SubElement(trips_root, 'trip')
                trip_ret.set('id', f"private_car_{i // 2}_return")
                trip_ret.set('depart', f'{return_time:.1f}')
                trip_ret.set('from', d_go.destination_edge)
                trip_ret.set('to', d_go.origin_edge)
                trip_ret.set('type', 'private_car')

    if sys.version_info >= (3, 9):
        ET.indent(trips_root, space="    ")
    ET.ElementTree(trips_root).write(temp_trips, encoding='utf-8', xml_declaration=True)

    ridehail_orders_file = os.path.join(paths.output_dir, "ridehail_demands.csv")
    od_generator._save_ridehail_demands(ridehail_demands, ridehail_orders_file)

    duarouter_exec = getattr(paths, 'duarouter', None) or shutil.which('duarouter') or 'duarouter'

    # duarouter命令使用临时文件作为输入
    duarouter_cmd = [
        duarouter_exec, '-n', net_file, '-t', temp_trips, '-o', rou_file,
        '--ignore-errors', 'true', '--repair', 'true', '--remove-loops', 'true'
    ]

    if run_command(duarouter_cmd, "转换trips为routes"):
        try:
            # 清理临时文件
            if os.path.exists(temp_trips):
                os.remove(temp_trips)
        except Exception:
            pass

        print("✓ 路由文件已生成（包含vType定义）")
        return True
    else:
        return False


def create_parking_facilities(net_file, config, paths):
    """创建停车位 - 只在高需求区生成"""
    try:
        tree = ET.parse(net_file)
        root = tree.getroot()
    except (ET.ParseError, FileNotFoundError) as e:
        print(f"✗ 解析网络文件失败: {e}")
        return {}

    additional_root = ET.Element('additional')
    parking_info = {}
    spot_counter = 0
    max_spots = 3000

    high_demand_types = ['highway.residential', 'highway.tertiary', 'highway.secondary']

    for edge in root.findall('edge'):
        if spot_counter >= max_spots: break
        if edge.get('function') == 'internal': continue

        edge_id = edge.get('id')
        edge_type = edge.get('type', '')

        if not any(demand_type in edge_type for demand_type in high_demand_types):
            continue

        lanes = edge.findall('lane')
        if not lanes: continue

        lane = lanes[0]
        lane_id = lane.get('id')
        length = float(lane.get('length', 0))

        if length < 150: continue

        num_spots = min(3, max(1, int(length / 200)))
        spot_length = 7.0

        for i in range(num_spots):
            if spot_counter >= max_spots: break
            spot_id = f"parkingSpot_{edge_id}_{i}"
            pos = 50 + i * 100
            if pos + spot_length < length - 20:
                parking = ET.SubElement(additional_root, 'parkingArea')
                parking.set('id', spot_id)
                parking.set('lane', lane_id)
                parking.set('startPos', str(pos))
                parking.set('endPos', str(pos + spot_length))
                parking.set('roadsideCapacity', '1')
                spot_counter += 1
                parking_info[spot_id] = {
                    'parking_id': spot_id, 'edge_id': edge_id, 'lane_id': lane_id,
                    'position': pos, 'capacity': 1
                }

    if sys.version_info >= (3, 9): ET.indent(additional_root, space="    ")
    ET.ElementTree(additional_root).write(paths.additional_file, encoding='utf-8', xml_declaration=True)
    print(f"创建了 {spot_counter} 个停车位（仅在高需求区域）")
    return parking_info


def create_sumo_config(cfg_file, net_file, rou_file, config, paths):
    """创建SUMO配置文件 - 启用自动路由"""
    print(f"\n--- 步骤: 创建SUMO配置文件 ---")
    config_root = ET.Element('configuration')

    input_sec = ET.SubElement(config_root, 'input')
    ET.SubElement(input_sec, 'net-file', value=os.path.basename(net_file))
    ET.SubElement(input_sec, 'route-files', value=os.path.basename(rou_file))
    if os.path.exists(paths.additional_file) and os.path.getsize(paths.additional_file) > 0:
        ET.SubElement(input_sec, 'additional-files', value=os.path.basename(paths.additional_file))

    time_sec = ET.SubElement(config_root, 'time')
    ET.SubElement(time_sec, 'begin', value='0')
    ET.SubElement(time_sec, 'end', value=str(config.simulation_duration))

    processing_sec = ET.SubElement(config_root, 'processing')
    ET.SubElement(processing_sec, 'ignore-route-errors', value='true')
    ET.SubElement(processing_sec, 'routing-algorithm', value='dijkstra')
    ET.SubElement(processing_sec, 'device.rerouting.probability', value='1')
    ET.SubElement(processing_sec, 'tripinfo-output.write-unfinished', value='true')

    gui_sec = ET.SubElement(config_root, 'gui_only')
    ET.SubElement(gui_sec, 'gui-settings-file', value='gui.settings.xml')

    if sys.version_info >= (3, 9): ET.indent(config_root, space="    ")
    ET.ElementTree(config_root).write(cfg_file, encoding='utf-8', xml_declaration=True)
    print(f"✓ 配置文件已生成（启用自动路由）")
    return True


def create_gui_settings(gui_settings_file):
    """创建GUI设置文件"""
    gui_root = ET.Element('viewsettings')
    ET.SubElement(gui_root, 'viewport', zoom='150')
    ET.SubElement(gui_root, 'vehicles', {
        'vehicleQuality': '2', 'minVehicleSize': '5', 'vehicleExaggeration': '2.5', 'vehicleColorer': 'given'
    })
    if sys.version_info >= (3, 9): ET.indent(gui_root, space="    ")
    ET.ElementTree(gui_root).write(gui_settings_file, encoding='utf-8', xml_declaration=True)


def test_generated_files(paths):
    """测试生成的文件"""
    print(f"\n--- 测试生成的文件 ---")
    all_ok = True
    files_to_test = [(paths.net_file, "网络"), (paths.rou_file, "路由"), (paths.cfg_file, "配置"),
                     (paths.additional_file, "停车设施")]
    for file_path, name in files_to_test:
        try:
            if os.path.exists(file_path):
                if os.path.getsize(file_path) > 0:
                    ET.parse(file_path)
                    print(f"✓ {name}文件正常")
                else:
                    if name == "停车设施":
                        print(f"✓ {name}文件为空，跳过")
                    else:
                        print(f"✗ {name}文件为空")
                        all_ok = False
            else:
                print(f"✗ {name}文件不存在")
                all_ok = False
        except Exception as e:
            print(f"✗ {name}文件错误: {e}")
            all_ok = False
    return all_ok