import json
import time
import argparse
import os
import logging
import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

from utils import reserve, get_user_credentials

get_current_time = lambda action: (
    time.strftime("%H:%M:%S", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%H:%M:%S", time.localtime(time.time()))
)
get_current_dayofweek = lambda action: (
    time.strftime("%A", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%A", time.localtime(time.time()))
)

SLEEPTIME = 0.2  # 每次抢座的间隔
RESERVE_TARGET_TIME = "22:00:00"  # 预约开始的目标时间（北京时间）
ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 5  # 最大尝试次数
RESERVE_NEXT_DAY = False  # 预约明天而不是今天的

def pre_login_users(users, usernames, passwords, action):
    """提前登录所有用户"""
    logged_sessions = []
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            logged_sessions.append(None)
            continue
            
        logging.info(f"User {username}: 提前登录中...")
        
        # 创建预约会话并登录
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        login_success, msg = s.login(username, password)
        
        if login_success:
            s.requests.headers.update({"Host": "office.chaoxing.com"})
            logged_sessions.append(s)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
    
    return logged_sessions

def wait_for_target_time(target_time, action):
    """等待到达目标时间"""
    current_time = get_current_time(action)
    
    if current_time < target_time:
        # 计算等待时间
        current_dt = datetime.datetime.strptime(current_time, "%H:%M:%S")
        target_dt = datetime.datetime.strptime(target_time, "%H:%M:%S")
        
        # 如果目标时间是第二天
        if target_dt < current_dt:
            target_dt += datetime.timedelta(days=1)
        
        wait_seconds = (target_dt - current_dt).total_seconds()
        logging.info(f"距离目标时间 {target_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
        
        time.sleep(wait_seconds)
    
    logging.info(f"到达目标时间 {target_time}（北京时间），开始预约")

def start_reservation(users, logged_sessions, action):
    """开始预约"""
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        
        if current_dayofweek not in daysofweek:
            continue
            
        s = logged_sessions[index]
        if s is None:
            continue
            
        # 处理多个时间段的预约
        if isinstance(times[0], list):  # 多个时间段
            for time_slot in times:
                logging.info(f"开始预约 - 用户 {username} -- {time_slot} -- 座位 {seatid}")
                s.submit([time_slot[0], time_slot[1]], roomid, seatid, action)
        else:  # 单个时间段
            logging.info(f"开始预约 - 用户 {username} -- {times} -- 座位 {seatid}")
            s.submit(times, roomid, seatid, action)

def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    # 提前登录所有用户
    logged_sessions = pre_login_users(users, usernames, passwords, action)
    
    # 等待目标时间
    wait_for_target_time(RESERVE_TARGET_TIME, action)
    
    # 开始预约
    start_reservation(users, logged_sessions, action)

def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nRESERVE_TARGET_TIME: {RESERVE_TARGET_TIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    logging.info(f"Debug Mode start! , action {'on' if action else 'off'}")
    
    if action:
        usernames, passwords = get_user_credentials(action)
    
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        suc = s.submit(times, roomid, seatid, action)
        if suc:
            return

def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)

if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
