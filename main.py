import json
import time
import argparse
import os
import logging
import concurrent.futures
from threading import Lock

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

# 优化后的参数设置
SLEEPTIME = 0.05  # 大幅减少等待时间，从0.2秒降到0.05秒
ENDTIME = "022:01:00"
ENABLE_SLIDER = True
MAX_ATTEMPT = 3  # 减少单个座位最大尝试次数，避免浪费时间
RESERVE_NEXT_DAY = False

# 新增：并发相关参数
MAX_WORKERS = 3  # 最大并发线程数
success_lock = Lock()  # 用于线程安全的成功状态更新

def reserve_single_user(user_data):
    """单用户预约函数，用于并发执行"""
    index, user, username, password, action, current_dayofweek = user_data
    
    user_username, user_password, times, roomid, seatid, daysofweek = user.values()
    if action:
        user_username, user_password = username, password
        
    if current_dayofweek not in daysofweek:
        logging.info(f"User {index}: Today not set to reserve")
        return index, False
        
    logging.info(f"----------- {user_username} -- {times} -- {seatid} try -----------")
    
    # 为每个用户创建独立的reserve实例，避免共享状态问题
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    
    try:
        s.get_login_status()
        s.login(user_username, user_password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        suc = s.submit(times, roomid, seatid, action)
        return index, suc
    except Exception as e:
        logging.error(f"User {index} error: {e}")
        return index, False

def login_and_reserve_parallel(users, usernames, passwords, action, success_list=None):
    """并发版本的登录和预约函数"""
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    
    if action and len(usernames.split(",")) != len(users):
        raise Exception("user number should match the number of config")
    
    if success_list is None:
        success_list = [False] * len(users)
    
    current_dayofweek = get_current_dayofweek(action)
    
    # 准备并发任务数据
    tasks = []
    for index, user in enumerate(users):
        if success_list[index]:
            continue  # 跳过已成功的用户
            
        username = usernames.split(",")[index] if action else None
        password = passwords.split(",")[index] if action else None
        
        tasks.append((index, user, username, password, action, current_dayofweek))
    
    # 并发执行
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_index = {
            executor.submit(reserve_single_user, task): task[0] 
            for task in tasks
        }
        
        for future in concurrent.futures.as_completed(future_to_index):
            index, suc = future.result()
            with success_lock:
                success_list[index] = suc
    
    return success_list

def login_and_reserve(users, usernames, passwords, action, success_list=None):
    """原有的串行版本，保持向后兼容"""
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    if action and len(usernames.split(",")) != len(users):
        raise Exception("user number should match the number of config")
    if success_list is None:
        success_list = [False] * len(users)
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
            continue
        if not success_list[index]:
            logging.info(
                f"----------- {username} -- {times} -- {seatid} try -----------"
            )
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
            success_list[index] = suc
    return success_list

def main(users, action=False, use_parallel=True):
    """主函数，新增并发开关"""
    current_time = get_current_time(action)
    logging.info(f"start time {current_time}, action {'on' if action else 'off'}, parallel {'on' if use_parallel else 'off'}")
    attempt_times = 0
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    success_list = None
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )
    
    # 选择使用并发还是串行版本
    reserve_func = login_and_reserve_parallel if use_parallel else login_and_reserve
    
    while current_time < ENDTIME:
        attempt_times += 1
        try:
            success_list = reserve_func(
                users, usernames, passwords, action, success_list
            )
        except Exception as e:
            logging.error(f"An error occurred: {e}")
            
        logging.info(
            f"attempt time {attempt_times}, time now {current_time}, success list {success_list}"
        )
        current_time = get_current_time(action)
        
        if success_list and sum(success_list) == today_reservation_num:
            logging.info(f"reserved successfully!")
            return
            
        # 减少循环间隔
        time.sleep(0.01)

def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")
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
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="disable parallel processing for compatibility",
    )
    args = parser.parse_args()
    
    func_dict = {"reserve": lambda users, action: main(users, action, not args.no_parallel), 
                 "debug": debug, 
                 "room": get_roomid}
    
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
