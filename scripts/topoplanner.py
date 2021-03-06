#!/usr/bin/env python

"""
    Topological planner
"""

import argparse
import random
import yaml
import rospy
import networkx as nx
import numpy as np
import webcolors

from termcolor import colored
from robot import Robot
from threading import Thread
from geometry_msgs.msg import PoseStamped, Pose, Point, Quaternion
from multirobot_interference.msg import *
from destination import Destination
from idleness_analysis import IdlenessLogger
from std_msgs.msg import Header
from nav_msgs.srv import GetPlan


class Planner(object):
    def __init__(self, adjlist, environment, yaml):
        self.graph = nx.read_adjlist(adjlist, delimiter=', ', nodetype=str)
        self.yaml = yaml
        self.environment = environment  # house, office ...
        self.logging = yaml['logging']  # bool, whether to print colored logs or not
        
        self.occupied_dests = []
        self.destinations = []
        for n in rospy.get_param(yaml['interest_points']):
            d = Destination(
                name=n['name'],
                pose=Pose(
                    Point(
                        n['pose']['position']['x'],
                        n['pose']['position']['y'],
                        n['pose']['position']['z']
                    ),
                    Quaternion(0, 0, 0, 1)
                )
            )
            self.destinations.append(d)

        self.robots = []
        self.available_robots = []
        self.temp_busy = []
        
        while not rospy.has_param(yaml['namespaces_topic']):
            rospy.logwarn("Waiting for robot namespaces to be published as param under %s" % yaml['namespaces_topic'])
            rospy.sleep(5)
            
        robot_namespaces = rospy.get_param(yaml['namespaces_topic'])
        for ns, color in robot_namespaces.items():
            rgba = color.strip("'").split(' ')
            rgba = [int(float(i)) * 255 for i in rgba]
            _color = webcolors.rgb_to_name(
                (rgba[0], rgba[1], rgba[2])
            )
            if ns.startswith('/'):
                self.robots.append(Robot(ns=ns, color=_color))
            else:
                self.robots.append(Robot(ns='/' + ns, color=_color))

        # ---------------
        self.start_threads()
        # self.update_robot_state()  # can be threaded in self.start_threads()
        
    def start_threads(self):
        dests_logger = Thread(target=self.destinations_log)
        dests_logger.start()
        
        dests_state_updater = Thread(target=self.update_available_dests)
        dests_state_updater.start()
        
        robot_state_updater = Thread(target=self.update_robot_state)
        robot_state_updater.start()
        
    def update_robot_state(self):
        for r in self.robots:
            rospy.Subscriber(r.ns+self.yaml['robot_state'], RobotState, self.on_robot_state)
        
    def on_robot_state(self, msg):
        temp = Robot(
            ns=msg.robot_name,
            state=msg.state,
            c_goal=msg.current_goal,
            l_goal=msg.latest_goal,
            final_goal=msg.final_goal,
            aff=msg.afference,
            dist=msg.distance
        )

        for r in self.robots:
            if r.ns == temp.ns:
                r.state = temp.state
                r.current_goal = temp.current_goal
                r.latest_goal = temp.latest_goal
                r.final_goal = temp.final_goal
                r.afference = temp.afference
                r.distance = temp.distance

                if r.state == 'ready' and r not in self.available_robots and r not in self.temp_busy:
                    self.available_robots.append(r)
                    self.log('%s is available' % r.ns, 'blue', attrs=['bold'])
                break
        
    def debug(self):
        # crash test
        # start_1 = 'WayPoint2'; goal_1 = 'WayPoint1'
        # start_2 = 'WayPoint1'; goal_2 = 'WayPoint2'
        start_1 = 'WayPoint2'; goal_1 = 'WayPoint4'
        start_2 = 'WayPoint1'; goal_2 = 'WayPoint7'

        path_rbt1 = self.find_path(start_1, goal_1)
        path_rbt2 = self.find_path(start_2, goal_2)

        tp_rbt1 = self.build_topopath(path_rbt1)
        tp_rbt2 = self.build_topopath(path_rbt2)

        while not self.available_robots:
            rospy.sleep(1)

        self.publish_path(tp_rbt1, '/robot_1')
        self.publish_path(tp_rbt2, '/robot_2')

        rospy.loginfo('Goal debug consegnati')

        rospy.spin()

    def _get_node_by_name(self, name):
        """
        :param (str) name: name of the destination
        :return: (Destination) object matching in name
        """
        for n in self.destinations:
            if n.name == name:
                return n

    @staticmethod
    def build_ipoint_msg(node):
        header = Header()
        header.stamp = rospy.Time.now()
        header.frame_id = 'map'
    
        pose = PoseStamped()
        pose.header = header
        pose.pose = node.pose
    
        return ToponavIpoint(pose, node.name)

    def build_topopath(self, path):
        """
        :param (list) path: path whose members are strings alike "WayPointX"
        :return: topopath msg
        """
        toponav_ipoints = []
        for p in path:
            node = self._get_node_by_name(p)
            ipoint = self.build_ipoint_msg(node)
            toponav_ipoints.append(ipoint)
        toponav_ipoints.pop(0)  # removes source
    
        return RobotTopopath(toponav_ipoints)

    def update_available_dests(self):
        """
        based on the final AND latest goals of the robots
        (contained in the RobotState msg), updates the status
        of the destinations
        """
        
        rate = rospy.Rate(5)
        try:
            while not rospy.is_shutdown():
                latest_goals = []
                final_goals = []
                for r in self.robots:
                    latest_goals.append(r.latest_goal)
                    final_goals.append(r.final_goal)
                    
                for d in self.destinations:
                    updated = False
                    for f_goal in final_goals:
                        if f_goal != 'None' and d.name == f_goal:
                            d.available = False
                            updated = True
                            break
                       
                    if not updated:  # updated means that destination found a match in previous iteration
                        for l_goal in latest_goals:
                            if l_goal != 'None' and d.name == l_goal and l_goal not in final_goals:  # <-- avoids conflict
                                d.available = True
                                
                                if d in self.occupied_dests:
                                    self.occupied_dests.remove(d)
                                break
                    
                rate.sleep()
        except rospy.ROSInterruptException:
            pass
    
    def find_path(self, source, dest):
        """
        :param source: source of planning (type: str, indicates a WayPoint)
        :param dest: destination of planning (type: str, indicates a WayPoint)
        :return: list of WayPoints (type:str) to traverse in order to reach the goal
        """
        try:
            return nx.dijkstra_path(self.graph, source, dest)
        except nx.NetworkXNoPath as e:
            rospy.logerr(str(e))

    def dispatch_goals(self):
        robots_num = len(self.robots)

        try:
            while not rospy.is_shutdown():
                if not self.available_robots:
                    rospy.sleep(1)
                else:
                    robot = self.available_robots.pop(0)
                    
                    # if another RobotState msg for the same
                    # robot arrives, if the robot is in
                    # this list, the new msg gets ignored
                    self.temp_busy.append(robot)
                    
                    source = self._get_node_by_name(robot.afference)
                    
                    while not self.has_destination(source):
                        rospy.sleep(0.1)
                    dest = self.choose_destination(robot.ns, source)
                    estim_idl, path_len = self.estimate_idleness(robot.ns, source, dest)
                    dest.estim_idl = estim_idl
                    dest.path_len = float(path_len)
                    
                    path = self.find_path(source=source.name, dest=dest.name)
                    topopath = self.build_topopath(path)
                    
                    self.publish_path(topopath, robot.ns)
                    self.log('%s: %s -> %s' % (robot.ns, source, dest.name), 'red')
                    self.temp_busy.remove(robot)
        except rospy.ROSInterruptException:
            pass
        
    def has_destination(self, src):
        """
        returns True if there is at least a dest that
        is free, otherwise returns False
        """
        for d in self.destinations:
            if (
                d.available and
                d.name != src.name and
                d not in self.occupied_dests
            ):
                return True
        
        return False

    def choose_destination(self, robot_ns, src):
        """
        chooses a valid destination for the robot with
        ns == robot_ns
        
        :param (str) robot_ns: ns the robot
        :param (Destination) src: afference of the robot
        :return: (Destination) end destination for the robot
        """
        curr_idl = -1
        selected = []
        
        for d in self.destinations:
            if (
                d.available and
                d.name != src.name and
                d.get_true_idleness() >= curr_idl and
                d not in self.occupied_dests
            ):
                selected.append(d)
                curr_idl = d.get_true_idleness()
        
        if selected:
            dest = random.choice(selected)
            self.occupied_dests.append(dest)
            
            return dest
    
    def estimate_idleness(self, robot_ns, source, dest):
        """
        provides an estimate for the idleness of dest, supposing
        that robot_ns travels at constant maximum speed from source to dest
        """
        make_plan_topic = robot_ns + self.yaml['make_plan']
        
        rospy.wait_for_service(make_plan_topic)
        try:
            make_plan = rospy.ServiceProxy(make_plan_topic, GetPlan)
    
            h1 = Header()
            h1.frame_id = 'map'
            h1.stamp = rospy.Time.now()

            h2 = Header()
            h2.frame_id = 'map'
            h2.stamp = rospy.Time.now()
            
            response = make_plan(PoseStamped(h1, source.pose),
                                 PoseStamped(h2, dest.pose), self.yaml['ipoint_radius'])

            plan = response.plan
            path_length = 0
            estimate = -1.0
            for i in range(len(plan.poses) - 1):
                position_a_x = plan.poses[i].pose.position.x
                position_b_x = plan.poses[i + 1].pose.position.x
                position_a_y = plan.poses[i].pose.position.y
                position_b_y = plan.poses[i + 1].pose.position.y
        
                path_length += np.sqrt(
                    np.power((position_b_x - position_a_x), 2) + np.power((position_b_y - position_a_y), 2)
                )
    
            estimate = round(path_length / self.yaml['robot_max_speed'], 2)
            
            return estimate, round(path_length, 3)
        except rospy.ServiceException as e:
            print "Make_plan call failed: %s" % e
        
    def destinations_log(self):
        """
        debug function that publishes a msg containing
        the status of the destinations
        
        destinations_debugger.py subscribes to this topic
        """
        pub = rospy.Publisher(self.yaml['destinations_log'], DestinationDebug, queue_size=30)
        rate = rospy.Rate(1)
        
        try:
            while not rospy.is_shutdown():
                for d in self.destinations:
                    msg = DestinationDebug()
                    msg.available = d.available
                    msg.name = d.name
                    msg.idleness = d.get_true_idleness()
                    
                    pub.publish(msg)
                rate.sleep()
        except rospy.ROSInterruptException:
            pass
        
    def publish_path(self, path, target_robot):
        """
        publishes the topopath under the target_robot's namespace
        """
        pub = rospy.Publisher(target_robot + self.yaml['robot_topopath'], RobotTopopath, queue_size=10)
        while pub.get_num_connections() < 1:
            rospy.sleep(0.1)
        pub.publish(path)
        
    def log(self, msg, color, attrs=None):
        """
        prints to STDOUT useful logs about what's happening
        """
        if self.logging:
            print colored(msg, color=color, attrs=attrs)
        
    def on_shutdown(self):
        """
        when roscore is shutting down, dumps the registered idlenesses
        """
        if self.yaml['simulation_dump']:
            dest_logger = IdlenessLogger(dest_list=self.destinations,
                                         robots_num=len(self.robots), environment=self.environment)
            if self.yaml['simulation_confirm_gui']:
                dest_logger.show_confirm_gui()
            else:
                dest_logger.write_statfile()
        else:
            rospy.logwarn('Dump file has not been saved.')
    
    
def parse_yaml(dir):
    f = open(dir, 'r')
    return yaml.safe_load(f)

    
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adjlist', type=str, required=True)
    parser.add_argument('--environment', type=str, required=True)
    parser.add_argument('--yaml', type=str, help='File where used topics_yaml are saved')
    args, unknown = parser.parse_known_args()
    
    return args


if __name__ == '__main__':
    rospy.init_node('topoplanner')
    
    args = parse_args()
    yaml = parse_yaml(args.yaml)
    
    planner = Planner(args.adjlist, environment=args.environment, yaml=yaml)
    rospy.on_shutdown(planner.on_shutdown)  # dumps idlenesses of destinations
    
    if yaml['simulation_time_measure'] == 'minutes':
        minutes = yaml['simulation_duration']
        rospy.Timer(period=rospy.Duration(60*minutes), callback=rospy.signal_shutdown, oneshot=True)
    elif yaml['simulation_time_measure'] == 'seconds':
        seconds = yaml['simulation_duration']
        rospy.Timer(period=rospy.Duration(seconds), callback=rospy.signal_shutdown, oneshot=True)

    rospy.loginfo('Simulation started')
    # planner.debug()
    planner.dispatch_goals()
