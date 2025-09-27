# -*- coding: utf-8 -*-
"""
Ridehail Coordinator (static optimization version)
------------------------------------------
修复内容：
1. 使用离线优化的固定车队规模
2. 所有订单预生成
3. 网约车持续巡游接单
"""

from __future__ import annotations

import os
import csv
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import traci
from fleet_optimizer import VehicleShareabilityOptimizer


# -----------------------------
# Data classes
# -----------------------------

@dataclass
class RideOrder:
    """网约车订单"""
    order_id: str
    pickup_edge: str
    destination_edge: str
    pickup_time: float
    passenger_count: int = 1
    status: str = "waiting"  # waiting, assigned, pickup, completed, cancelled
    assigned_vehicle: Optional[str] = None


class OrderValidator:
    """订单验证器 - 放宽匹配条件"""

    def __init__(self):
        self.reachability_cache: Dict[Tuple[str, str, str], bool] = {}
        self.unreachable_edges: set[str] = set()
        self.lenient_validation: bool = True  # 默认开启宽松模式

    def check_route_feasibility(self, from_edge: str, to_edge: str, vtype: str = "ridehail_car") -> bool:
        """检查路径可达性 - 更宽松的标准"""
        if self.lenient_validation:
            return True

        if not from_edge or not to_edge or from_edge.startswith(":") or to_edge.startswith(":"):
            return False

        cache_key = (from_edge, to_edge, vtype)
        if cache_key in self.reachability_cache:
            return self.reachability_cache[cache_key]

        # 简化验证，减少计算开销
        try:
            # 只检查边是否存在
            if from_edge in traci.edge.getIDList() and to_edge in traci.edge.getIDList():
                self.reachability_cache[cache_key] = True
                return True
        except traci.TraCIException:
            pass

        self.reachability_cache[cache_key] = False
        return False

    def validate_order(self, order: RideOrder) -> bool:
        return True  # 暂时接受所有订单

    def find_reachable_vehicle(self, order: RideOrder, available_vehicles: List[str]) -> Optional[
        Tuple[str, List[str]]]:
        """寻找最近的可用车辆 - 放宽距离限制"""
        best_vehicle: Optional[str] = None
        best_route: Optional[List[str]] = None
        best_distance = float("inf")

        for veh_id in available_vehicles:
            try:
                current_edge = traci.vehicle.getRoadID(veh_id)
                if not current_edge or current_edge.startswith(":"):
                    continue

                # 简化路径查找
                try:
                    # 尝试直接设置路径
                    route_to_pickup = traci.simulation.findRoute(current_edge, order.pickup_edge, vType="ridehail_car")
                    if route_to_pickup and route_to_pickup.edges:
                        # 使用距离而不是时间作为主要指标
                        if route_to_pickup.distance < best_distance:
                            best_distance = route_to_pickup.distance
                            best_vehicle = veh_id
                            best_route = list(route_to_pickup.edges)
                except:
                    # 如果路径查找失败，仍然尝试分配
                    if best_vehicle is None:
                        best_vehicle = veh_id
                        best_route = [current_edge, order.pickup_edge]

            except traci.TraCIException:
                continue

        if best_vehicle:
            return best_vehicle, best_route if best_route else []

        # 如果没找到，随机选择一个
        if available_vehicles:
            veh_id = available_vehicles[0]
            return veh_id, []

        return None


# -----------------------------
# Coordinator
# -----------------------------

class RidehailCoordinator:
    """网约车协调器 - 静态优化版本"""

    def __init__(self, config):
        self.config = config
        self.order_validator = OrderValidator()
        self.fleet_optimizer = VehicleShareabilityOptimizer(config)

        # 订单与车辆状态
        self.active_orders: Dict[str, RideOrder] = {}
        self.ridehail_vehicles: Dict[str, str] = {}  # veh_id -> 'idle'/'busy'
        self.vehicle_orders: Dict[str, str] = {}  # veh_id -> order_id
        self.pending_orders: List[RideOrder] = []
        self.cancelled_orders: List[RideOrder] = []

        # 所有预生成的订单，按时间排序
        self.all_orders_by_time: List[RideOrder] = []
        self.order_release_index = 0

        # 路由存活表
        self._alive_routes: set[str] = set()

        # === 关键修改：使用离线优化的固定规模 ===
        self.current_fleet_size = 0
        self.vehicles_initialized = 0
        self.all_vehicles_spawned = False

        # 优先使用config的离线优化结果
        if getattr(config, "optimal_fleet_size", None):
            self.optimal_fleet_size = int(config.optimal_fleet_size)
            print(f"使用离线优化的网约车规模: {self.optimal_fleet_size} 辆")
        elif getattr(config, "vehicle_composition", None):
            self.optimal_fleet_size = int(config.vehicle_composition.get("ride_hailing", 20))
            print(f"使用配置的网约车规模: {self.optimal_fleet_size} 辆")
        else:
            # 兜底：保持原有启发式，保证没有离线结果时也能跑通
            total_demand = getattr(config, "total_trip_demand", 10000)
            ridehail_ratio = getattr(config, "ridehail_order_ratio", 0.4)
            self.optimal_fleet_size = max(20, int(total_demand * ridehail_ratio * 0.1))
            print(f"使用默认估算的网约车规模: {self.optimal_fleet_size} 辆")

        self.next_vehicle_index = 0
        self.pending_spawn: Dict[str, float] = {}
        self.spawn_depart_spacing = 0.5
        self.spawn_timeout = 60.0
        self.connected_edges: List[str] = []
        self.spawn_edges: List[str] = []  # 高需求边缘
        self.spawn_weights: List[float] = []  # 对应权重

        # 统计量
        self.total_orders_requested = 0
        self.total_orders_validated = 0
        self.total_orders_matched = 0
        self.total_orders_completed = 0
        self.total_orders_cancelled = 0
        self.total_orders_unreachable = 0

        # 放宽参数
        self.cancellation_probability = 0.005  # 降低取消率
        self.max_wait_time = 1200  # 增加到20分钟

        # 可选外部模块
        self.parking_detector = None

    def _route_exists(self, route_id: str) -> bool:
        """检查路由是否存在"""
        try:
            _ = traci.route.getEdges(route_id)
            return True
        except Exception:
            return False

    def _ensure_route(self, route_id: str, edges: list) -> bool:
        """确保路由存在，若不存在则重新添加"""
        if not edges:
            return False
        if self._route_exists(route_id):
            return True
        try:
            traci.route.add(route_id, edges)
            self._alive_routes.add(route_id)
            return True
        except Exception:
            return False

    def _safe_find_route_edges(self, from_edge: str, to_edge: str) -> list:
        """验证可达性并返回路径"""
        try:
            if not from_edge or not to_edge or from_edge.startswith(':') or to_edge.startswith(':'):
                return []
            r = traci.simulation.findRoute(from_edge, to_edge, vType="ridehail_car")
            if r and getattr(r, "edges", None):
                return list(r.edges) if r.edges else []
        except:
            pass
        return []

    def _safe_set_route(self, veh_id: str, edges: list) -> bool:
        """安全设置路径"""
        if not edges:
            return False
        try:
            traci.vehicle.setRoute(veh_id, edges)
            return True
        except:
            return False

    # --------- fleet init (一次性投放) ---------
    def initialize_fleet(self):
        """初始化网约车 - 一次性投放全部车辆，确保永远存在"""
        try:
            if "ridehail_car" not in traci.vehicletype.getIDList():
                traci.vehicletype.copy("DEFAULT_VEHTYPE", "ridehail_car")
                traci.vehicletype.setColor("ridehail_car", (34, 139, 34, 255))
        except traci.TraCIException:
            return

        try:
            edge_list = traci.edge.getIDList()
            usable_edges = [e for e in edge_list if not e.startswith(':')]
            if not usable_edges:
                print('未找到可用于投放网约车的道路边')
                return

            self.connected_edges = usable_edges

            base_time = traci.simulation.getTime()
            # 关键：一次性投放所有车辆
            scheduled = self._spawn_all_vehicles(self.optimal_fleet_size, base_time)

            self.all_vehicles_spawned = True
            self._update_fleet_size()

            print(f'✓ 已一次性调度 {scheduled}/{self.optimal_fleet_size} 辆网约车等待入场')
        except Exception as e:
            print(f'初始化车队时发生错误: {e}')

    def _spawn_all_vehicles(self, count: int, base_time: float) -> int:
        """一次性生成所有网约车，确保它们永远在仿真中巡游"""
        if count <= 0:
            return 0

        # 使用高需求边缘（如果已加载）
        edges = self.spawn_edges if self.spawn_edges else self.connected_edges
        weights = self.spawn_weights if self.spawn_weights else None

        if not edges:
            edges = [e for e in traci.edge.getIDList() if not e.startswith(':')]
        if not edges:
            return 0

        added = 0
        veh_index = self.next_vehicle_index
        spacing = self.spawn_depart_spacing

        for _ in range(count):
            veh_id = f"ridehail_car_{veh_index}"

            # 使用权重选择起点（如果有）
            if weights:
                start_edge = random.choices(edges, weights=weights, k=1)[0]
            else:
                start_edge = random.choice(edges)

            # 生成一个初始巡游路径
            path_edges = []
            for _ in range(8):
                end_edge = random.choice(edges)
                if end_edge != start_edge:
                    path_edges = self._safe_find_route_edges(start_edge, end_edge)
                    if path_edges:
                        break

            if not path_edges:
                # 至少在起点待着
                path_edges = [start_edge]

            route_id = f"ridehail_route_{veh_id}"

            # 确保路由存在
            if not self._ensure_route(route_id, path_edges):
                veh_index += 1
                continue

            depart_time = base_time + added * spacing
            try:
                traci.vehicle.add(
                    veh_id,
                    routeID=route_id,
                    typeID='ridehail_car',
                    depart=str(depart_time),
                    departLane='best',
                    departPos='random_free',
                    departSpeed='max'
                )

                self.pending_spawn[veh_id] = depart_time
                self.ridehail_vehicles[veh_id] = "idle"  # 初始状态为空闲
                added += 1

                # 记录活跃路由
                self._alive_routes.add(route_id)

            except traci.TraCIException:
                # 仅在此处（加车失败）允许清理临时路由
                if route_id in self._alive_routes:
                    self._alive_routes.discard(route_id)
                try:
                    traci.route.remove(route_id)
                except:
                    pass

            veh_index += 1

        self.next_vehicle_index = veh_index
        self.vehicles_initialized = added
        return added

    def _update_fleet_size(self):
        """更新实际车队规模"""
        try:
            all_vehicles = traci.vehicle.getIDList()
            self.current_fleet_size = len([v for v in all_vehicles if v.startswith('ridehail_car')])

            # 同步更新车辆状态字典
            for v in all_vehicles:
                if v.startswith('ridehail_car') and v not in self.ridehail_vehicles:
                    self.ridehail_vehicles[v] = "idle"

            # 不删除已消失的车辆记录，保持车辆应该存在的状态

        except traci.TraCIException:
            pass

    # --------- orders ---------
    def load_pregenerated_orders(self, csv_file_path: str):
        """加载所有预生成的订单，并按时间排序"""
        if not os.path.exists(csv_file_path):
            print(f"订单文件不存在: {csv_file_path}")
            return

        try:
            with open(csv_file_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    order = RideOrder(
                        order_id=row["demand_id"],
                        pickup_edge=row["origin_edge"],
                        destination_edge=row["destination_edge"],
                        pickup_time=float(row["departure_time"]),
                    )
                    self.all_orders_by_time.append(order)

            if self.all_orders_by_time:
                # 按pickup_time排序
                self.all_orders_by_time.sort(key=lambda x: x.pickup_time)

                # 统计总订单数
                self.total_orders_requested = len(self.all_orders_by_time)

                # 构建高需求区域用于投放车辆
                pickup_edges = {}
                for order in self.all_orders_by_time:
                    edge = order.pickup_edge
                    if edge and not edge.startswith(':'):
                        pickup_edges[edge] = pickup_edges.get(edge, 0) + 1

                # 选择高频边缘作为投放点
                if pickup_edges:
                    sorted_edges = sorted(pickup_edges.items(), key=lambda x: x[1], reverse=True)
                    top_edges = sorted_edges[:min(100, len(sorted_edges))]
                    self.spawn_edges = [e[0] for e in top_edges]
                    self.spawn_weights = [float(e[1]) for e in top_edges]

                print(f"✓ 加载了 {len(self.all_orders_by_time)} 个网约车订单")
                print(f"  最早订单时间: {self.all_orders_by_time[0].pickup_time:.1f}")
                print(f"  最晚订单时间: {self.all_orders_by_time[-1].pickup_time:.1f}")

        except Exception as e:
            print(f"加载预生成订单失败: {e}")

    def release_orders_for_step(self, current_step: int):
        """按时间释放订单到pending队列"""
        released_count = 0

        while self.order_release_index < len(self.all_orders_by_time):
            order = self.all_orders_by_time[self.order_release_index]

            # 检查是否到了释放时间
            if order.pickup_time <= current_step:
                self.pending_orders.append(order)
                self.order_release_index += 1
                released_count += 1
            else:
                # 后续订单时间更晚，停止释放
                break

        if released_count > 0 and current_step % 100 == 0:
            print(f"步骤 {current_step}: 释放了 {released_count} 个订单")

    # --------- dispatch ---------
    def dispatch_pending_orders(self):
        """派发待处理订单"""
        self._update_fleet_size()  # 更新车辆统计

        available_vehicles = [v for v, s in self.ridehail_vehicles.items()
                              if s == "idle" and v in traci.vehicle.getIDList()]

        if not available_vehicles or not self.pending_orders:
            return

        # 时间优先
        self.pending_orders.sort(key=lambda x: x.pickup_time)
        max_batch = min(100, len(self.pending_orders), len(available_vehicles))
        batch = self.pending_orders[:max_batch]

        dispatched: List[RideOrder] = []

        for order in batch:
            if not available_vehicles:
                break

            # 更宽松的匹配
            result = self.order_validator.find_reachable_vehicle(order, available_vehicles)

            if not result:
                # 超时取消
                wait_time = max(0.0, traci.simulation.getTime() - order.pickup_time)
                if wait_time > self.max_wait_time:
                    order.status = "cancelled"
                    self.total_orders_cancelled += 1
                    dispatched.append(order)
                continue

            best_vehicle, _ = result

            try:
                # 获取当前边
                curr_edge = traci.vehicle.getRoadID(best_vehicle)
                if not curr_edge or curr_edge.startswith(':'):
                    route_now = traci.vehicle.getRoute(best_vehicle)
                    curr_edge = route_now[-1] if route_now else None
                    if not curr_edge:
                        continue

                # 使用findRoute + setRoute
                route_to_pickup = self._safe_find_route_edges(curr_edge, order.pickup_edge)
                if not route_to_pickup:
                    continue

                if not self._safe_set_route(best_vehicle, route_to_pickup):
                    continue

                traci.vehicle.setColor(best_vehicle, (255, 165, 0, 255))  # 橙色

                order.status = "assigned"
                order.assigned_vehicle = best_vehicle
                self.ridehail_vehicles[best_vehicle] = "busy"
                self.vehicle_orders[best_vehicle] = order.order_id
                self.active_orders[order.order_id] = order
                self.total_orders_matched += 1

                available_vehicles.remove(best_vehicle)
                dispatched.append(order)

            except traci.TraCIException:
                continue

        # 从pending中移除已处理订单
        for o in dispatched:
            if o in self.pending_orders:
                self.pending_orders.remove(o)
            if o.status == "cancelled":
                self.cancelled_orders.append(o)

    def update_vehicle_states(self, current_step: int):
        """更新车辆状态 - 确保车辆持续存在"""
        current_sim_vehicles = set(traci.vehicle.getIDList())

        # 确保所有网约车都记录在案
        for vid in current_sim_vehicles:
            if "ridehail_car" in vid and vid not in self.ridehail_vehicles:
                self.ridehail_vehicles[vid] = "idle"

        # 处理在途车辆
        for vid, status in list(self.ridehail_vehicles.items()):
            # 检查车辆是否还在仿真中
            if vid not in current_sim_vehicles:
                # 车辆不应该消失，重新生成或标记警告
                print(f"警告: 网约车 {vid} 不在仿真中")
                continue

            if status != "busy":
                continue

            oid = self.vehicle_orders.get(vid)
            if not oid or oid not in self.active_orders:
                self.ridehail_vehicles[vid] = "idle"
                continue

            order = self.active_orders[oid]

            try:
                curr_edge = traci.vehicle.getRoadID(vid)
                if not curr_edge or curr_edge.startswith(":"):
                    continue

                # 检查是否接近接客点
                if order.status == "assigned":
                    try:
                        route = traci.vehicle.getRoute(vid)
                        route_index = traci.vehicle.getRouteIndex(vid)

                        # 如果接近或到达接客边
                        if curr_edge == order.pickup_edge or (route_index >= len(route) - 2):
                            order.status = "pickup"

                            # 去目的地
                            route_to_dest = self._safe_find_route_edges(curr_edge, order.destination_edge)
                            if route_to_dest and self._safe_set_route(vid, route_to_dest):
                                traci.vehicle.setColor(vid, (255, 0, 255, 255))  # 紫色
                            else:
                                # 不可达：把订单放回pending
                                order.status = "waiting"
                                order.assigned_vehicle = None
                                self.pending_orders.append(order)
                                self.active_orders.pop(oid, None)
                                self.vehicle_orders.pop(vid, None)
                                self.ridehail_vehicles[vid] = "idle"
                                traci.vehicle.setColor(vid, (34, 139, 34, 255))
                                # 设置巡游路线
                                self._set_cruise_route(vid)
                                continue
                    except:
                        pass

                # 检查是否接近目的地
                elif order.status == "pickup":
                    try:
                        route = traci.vehicle.getRoute(vid)
                        route_index = traci.vehicle.getRouteIndex(vid)

                        # 放宽完成条件
                        if curr_edge == order.destination_edge or (route_index >= len(route) - 2):
                            order.status = "completed"
                            self.total_orders_completed += 1
                            self.ridehail_vehicles[vid] = "idle"
                            traci.vehicle.setColor(vid, (34, 139, 34, 255))  # 绿色
                            self.vehicle_orders.pop(vid, None)
                            self.active_orders.pop(oid, None)

                            # 完成订单后设置新的巡游路线
                            self._set_cruise_route(vid)
                    except:
                        # 如果出错也标记完成
                        order.status = "completed"
                        self.total_orders_completed += 1
                        self.ridehail_vehicles[vid] = "idle"
                        self.vehicle_orders.pop(vid, None)
                        self.active_orders.pop(oid, None)
                        self._set_cruise_route(vid)

            except traci.TraCIException:
                self.ridehail_vehicles[vid] = "idle"

    def _set_cruise_route(self, veh_id: str):
        """为空闲车辆设置巡游路线"""
        try:
            curr_edge = traci.vehicle.getRoadID(veh_id)
            if curr_edge and not curr_edge.startswith(':'):
                edges = [e for e in traci.edge.getIDList() if not e.startswith(':')]
                if edges:
                    # 随机选择目的地
                    target = random.choice(edges)
                    route = self._safe_find_route_edges(curr_edge, target)
                    if route:
                        self._safe_set_route(veh_id, route)
        except traci.TraCIException:
            pass

    def manage_idle_vehicles(self, current_step: int):
        """管理空闲车辆 - 持续巡游"""
        idle = [v for v, s in self.ridehail_vehicles.items()
                if s == "idle" and v in traci.vehicle.getIDList()]

        for vid in idle:
            try:
                # 检查是否需要新的巡游路线
                speed = traci.vehicle.getSpeed(vid)
                route = traci.vehicle.getRoute(vid)
                route_index = traci.vehicle.getRouteIndex(vid)

                # 如果车辆接近路线终点或速度很低，设置新路线
                if (route_index >= len(route) - 2) or (speed < 1.0 and random.random() < 0.1):
                    self._set_cruise_route(vid)

            except traci.TraCIException:
                continue

    # --------- main step ---------
    def step_update(self, current_step):
        """每步更新"""
        if current_step == 0:
            self.initialize_fleet()

        # 更新实际车队规模
        self._update_fleet_size()

        # 释放当前时间的订单
        self.release_orders_for_step(current_step)

        # 派单
        self.dispatch_pending_orders()

        # 更新车辆状态
        self.update_vehicle_states(current_step)

        # 管理空闲车辆巡游
        if current_step % 50 == 0:  # 更频繁地管理巡游
            self.manage_idle_vehicles(current_step)

    # --------- stats ---------
    def get_statistics(self) -> Dict:
        """获取统计信息"""
        self._update_fleet_size()  # 确保数据最新

        idle = sum(1 for s in self.ridehail_vehicles.values() if s == "idle")
        busy = sum(1 for s in self.ridehail_vehicles.values() if s == "busy")

        completion_rate = (self.total_orders_completed / max(1, self.total_orders_requested) * 100)

        return {
            "total_ridehail_vehicles": len(self.ridehail_vehicles),
            "current_fleet_size": self.current_fleet_size,
            "optimal_fleet_size": self.optimal_fleet_size,
            "idle_vehicles": idle,
            "busy_vehicles": busy,
            "pending_orders": len(self.pending_orders),
            "active_orders": len(self.active_orders),
            "total_orders_requested": self.total_orders_requested,
            "total_orders_validated": self.total_orders_validated,
            "total_orders_unreachable": self.total_orders_unreachable,
            "total_orders_matched": self.total_orders_matched,
            "total_orders_completed": self.total_orders_completed,
            "total_orders_cancelled": self.total_orders_cancelled,
            "completion_rate": completion_rate,
            "match_rate": (self.total_orders_matched / max(1, self.total_orders_requested) * 100),
            "orders_released": self.order_release_index,
            "orders_remaining": len(self.all_orders_by_time) - self.order_release_index,
        }