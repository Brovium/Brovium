# src/terminal_parking.py
"""私家车终点停车场模块 - 管理私家车在目的地的停车位"""

import traci
import traci.constants
import random
import math
from typing import Dict, List, Set, Tuple, Optional


class TerminalParkingManager:
    """私家车终点停车场管理器"""

    def __init__(self, config):
        self.config = config
        self.parked_vehicles = set()  # 已停入停车场的车辆
        self.searching_vehicles: Dict[str, str] = {}  # 车辆ID -> 目标停车场ID
        self.sumo_parking_areas: Dict[str, Dict] = {}  # 存储由simulation_setup生成的SUMO停车场信息

    def set_sumo_parking_areas(self, parking_info: Dict):
        """从主程序接收SUMO停车场信息"""
        self.sumo_parking_areas = parking_info
        print(f"✓ 终点停车管理器已接收 {len(parking_info)} 个停车位的信息。")

    def find_and_assign_parking(self, veh_id: str, destination_edge: str):
        """为车辆寻找并分配一个停车位"""
        if veh_id in self.searching_vehicles or veh_id in self.parked_vehicles:
            return

        # 1. 优先在目的地道路上寻找停车位
        spot_id = self._find_parking_on_edge(destination_edge)

        # 2. 如果目的地没有，则在附近寻找最近的
        if not spot_id:
            spot_id = self._find_closest_available_spot(veh_id)

        if spot_id:
            target_edge = self.sumo_parking_areas[spot_id]['edge_id']
            try:
                # 命令车辆前往停车位所在的道路
                traci.vehicle.changeTarget(veh_id, target_edge)
                self.searching_vehicles[veh_id] = spot_id
                traci.vehicle.setColor(veh_id, (255, 255, 0, 255))  # 黄色，表示寻找中
                # print(f"车辆 {veh_id} 已分配停车位 {spot_id}，正在前往...")
            except traci.TraCIException:
                # 路径规划失败，放弃此次停车
                pass
        else:
            # 如果完全找不到车位，暂时放弃
            # print(f"车辆 {veh_id} 未找到可用停车位。")
            pass

    def _find_parking_on_edge(self, edge_id: str) -> Optional[str]:
        """在指定的道路上寻找一个可用的停车位"""
        spots_on_edge = [
            pid for pid, info in self.sumo_parking_areas.items() if info['edge_id'] == edge_id
        ]
        random.shuffle(spots_on_edge)  # 随机化选择，避免拥堵

        for spot_id in spots_on_edge:
            try:
                info = self.sumo_parking_areas[spot_id]
                if traci.parkingarea.getVehicleCount(spot_id) < info['capacity']:
                    return spot_id
            except traci.TraCIException:
                continue
        return None

    def _find_closest_available_spot(self, veh_id: str, search_radius: float = 1000.0) -> Optional[str]:
        """在车辆附近寻找最近的可用停车位"""
        try:
            x, y = traci.vehicle.getPosition(veh_id)
        except traci.TraCIException:
            return None

        available_spots = []
        for spot_id, info in self.sumo_parking_areas.items():
            try:
                if traci.parkingarea.getVehicleCount(spot_id) < info['capacity']:
                    # 简化距离计算，使用停车场的第一个坐标点
                    spot_pos = traci.lane.getShape(info['lane_id'])[0]
                    dist = math.sqrt((x - spot_pos[0]) ** 2 + (y - spot_pos[1]) ** 2)
                    if dist < search_radius:
                        available_spots.append((dist, spot_id))
            except traci.TraCIException:
                continue

        if not available_spots:
            return None

        # 返回距离最近的那个
        available_spots.sort(key=lambda item: item[0])
        return available_spots[0][1]

    def execute_parking_for_arrived_vehicle(self, veh_id: str):
        """当车辆到达目标停车场道路后，执行停车"""
        if veh_id not in self.searching_vehicles:
            return

        target_spot_id = self.searching_vehicles[veh_id]

        try:
            current_edge = traci.vehicle.getRoadID(veh_id)
            target_edge = self.sumo_parking_areas[target_spot_id]['edge_id']

            # 确认车辆已在正确的道路上
            if current_edge == target_edge:
                # 获取停车位位置信息
                spot_info = self.sumo_parking_areas[target_spot_id]
                current_pos = traci.vehicle.getLanePosition(veh_id)
                spot_pos = spot_info.get('position', 100)

                # 检查距离是否足够
                distance_to_spot = spot_pos - current_pos

                # 如果距离太近（小于20米），先继续前进
                if distance_to_spot < 20:
                    return  # 等待下一步再尝试

                traci.vehicle.setParkingAreaStop(
                    vehID=veh_id,
                    stopID=target_spot_id,
                    duration=self.config.terminal_parking_duration
                )
                # 更新状态
                self.parked_vehicles.add(veh_id)
                del self.searching_vehicles[veh_id]

        except traci.TraCIException:
            # 如果出错，放弃此次停车
            if veh_id in self.searching_vehicles:
                del self.searching_vehicles[veh_id]

    def get_parking_lot_statistics(self) -> Dict:
        """获取停车场统计信息"""
        occupied_spots = 0
        total_spots = sum(info.get('capacity', 0) for info in self.sumo_parking_areas.values())

        try:
            if traci.isLoaded():
                for parking_id in self.sumo_parking_areas:
                    occupied_spots += traci.parkingarea.getVehicleCount(parking_id)
        except traci.TraCIException:
            # 仿真结束时可能查询失败，使用内部记录估算
            occupied_spots = len(self.parked_vehicles)

        return {
            'total_parking_spots': total_spots,
            'occupied_parking_spots': occupied_spots,
            'parked_in_lots': len(self.parked_vehicles),
            'searching_vehicles': len(self.searching_vehicles),
            'available_spots': max(0, total_spots - occupied_spots)
        }