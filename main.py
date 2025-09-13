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

# 极速优化参数
SLEEPTIME = 0.005  # 进一步减少到5毫秒
ENDTIME = "07:01:00"
ENABLE_SLIDER = True
MAX_ATTEMPT = 2  # 减少到2次，避免浪费时间
RESERVE_NEXT_DAY = False

# 并发参数优化
MAX_WORKERS = 16  # 增加并发数
success_lock = Lock()

def reserve_single_user_ultra_fast(user_data):
    """超高速单用户预约"""
    index, user, username, password, action, current_dayofweek = user_data
    
    user_username, user_password, times, roomid, seatid, daysofweek = user.values()
    if action:
        user_username, user_password = username, password
        
    if current_dayofweek not in daysofweek:
        logging.info(f"用户 {index}: 今日无需预约")
        return index, False
        
    start_time = time.time()
    logging.info(f"🚀 用户 {user_username} 开始抢座 时段:{times} 座位:{seatid}")
    
    # 每个用户独立的reserve实例，使用极速参数
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    
    try:
        # 并行执行登录状态检查和登录（如果可能）
        s.get_login_status()
        login_success, login_msg = s.login(user_username, user_password)
        
        if not login_success:
            logging.error(f"用户 {index} 登录失败: {login_msg}")
            return index, False
            
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        
        suc = s.submit(times, roomid, seatid, action)
        
        elapsed = time.time() - start_time
        if suc:
            logging.info(f"✅ 用户 {index} 预约成功！耗时: {elapsed:.3f}秒")
        else:
            logging.info(f"❌ 用户 {index} 预约失败，耗时: {elapsed:.3f}秒")
            
        return index, suc
        
    except Exception as e:
        elapsed = time.time() - start_time
        logging.error(f"用户 {index} 异常: {e}, 耗时: {elapsed:.3f}秒")
        return index, False

def login_and_reserve_ultra_fast(users, usernames, passwords, action, success_list=None):
    """超高速并发预约"""
    logging.info(f"🔥 极速模式启动! 参数: SLEEPTIME={SLEEPTIME}, MAX_ATTEMPT={MAX_ATTEMPT}, MAX_WORKERS={MAX_WORKERS}")
    
    if action and len(usernames.split(",")) != len(users):
        raise Exception("用户数量不匹配配置文件数量")
    
    if success_list is None:
        success_list = [False] * len(users)
    
    current_dayofweek = get_current_dayofweek(action)
    
    # 准备任务，只处理未成功的用户
    tasks = []
    for index, user in enumerate(users):
        if success_list[index]:
            continue
            
        username = usernames.split(",")[index] if action else None
        password = passwords.split(",")[index] if action else None
        
        tasks.append((index, user, username, password, action, current_dayofweek))
    
    if not tasks:
        logging.info("所有用户已成功，无需处理")
        return success_list
    
    # 超高速并发执行
    start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_index = {
            executor.submit(reserve_single_user_ultra_fast, task): task[0] 
            for task in tasks
        }
        
        for future in concurrent.futures.as_completed(future_to_index):
            index, suc = future.result()
            with success_lock:
                success_list[index] = suc
                
            # 如果有成功的，立即记录
            if suc:
                elapsed = time.time() - start_time
                logging.info(f"🎯 首个成功！用户{index}, 总耗时: {elapsed:.3f}秒")
    
    total_elapsed = time.time() - start_time
    success_count = sum(success_list)
    logging.info(f"📊 本轮结果: {success_count}/{len(users)} 成功, 总耗时: {total_elapsed:.3f}秒")
    
    return success_list

# 保持原有函数兼容性
def login_and_reserve(users, usernames, passwords, action, success_list=None):
    """原版串行函数，兼容性保留"""
    return login_and_reserve_ultra_fast(users, usernames, passwords, action, success_list)

def main(users, action=False, use_ultra_fast=True):
    """主函数，默认启用超高速模式"""
    current_time = get_current_time(action)
    mode_str = "🚀 超高速模式" if use_ultra_fast else "🐌 兼容模式"
    logging.info(f"启动时间: {current_time}, Action: {'开启' if action else '关闭'}, 模式: {mode_str}")
    
    attempt_times = 0
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    success_list = None
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )
    
    logging.info(f"📋 今日需预约用户数: {today_reservation_num}")
    
    # 选择处理函数
    reserve_func = login_and_reserve_ultra_fast if use_ultra_fast else login_and_reserve_ultra_fast
    
    total_start_time = time.time()
    while current_time < ENDTIME:
        attempt_times += 1
        round_start_time = time.time()
        
        try:
            success_list = reserve_func(users, usernames, passwords, action, success_list)
        except Exception as e:
            logging.error(f"第 {attempt_times} 轮异常: {e}")
            
        round_elapsed = time.time() - round_start_time
        current_time = get_current_time(action)
        
        if success_list:
            success_count = sum(success_list)
            logging.info(f"🔄 第 {attempt_times} 轮: {success_count}/{today_reservation_num} 成功, 本轮耗时: {round_elapsed:.3f}秒, 当前时间: {current_time}")
            
            if success_count == today_reservation_num:
                total_elapsed = time.time() - total_start_time
                logging.info(f"🎉 全部预约成功！总耗时: {total_elapsed:.3f}秒, 轮次: {attempt_times}")
                return
        else:
            logging.info(f"🔄 第 {attempt_times} 轮: 处理中, 本轮耗时: {round_elapsed:.3f}秒, 当前时间: {current_time}")
        
        # 微小间隔，避免过度占用CPU
        time.sleep(0.001)
    
    total_elapsed = time.time() - total_start_time
    final_success = sum(success_list) if success_list else 0
    logging.info(f"⏰ 时间到！最终结果: {final_success}/{today_reservation_num} 成功, 总耗时: {total_elapsed:.3f}秒")

def debug(users, action=False):
    """调试模式，单用户测试"""
    logging.info(f"🔧 调试模式启动! SLEEPTIME={SLEEPTIME}, MAX_ATTEMPT={MAX_ATTEMPT}")
    
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
            logging.info("今日无需预约")
            continue
            
        logging.info(f"🎯 测试用户: {username} 时段: {times} 座位: {seatid}")
        
        start_time = time.time()
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        login_success, login_msg = s.login(username, password)
        
        if not login_success:
            logging.error(f"登录失败: {login_msg}")
            continue
            
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        suc = s.submit(times, roomid, seatid, action)
        
        elapsed = time.time() - start_time
        if suc:
            logging.info(f"✅ 调试成功！耗时: {elapsed:.3f}秒")
            return
        else:
            logging.info(f"❌ 调试失败，耗时: {elapsed:.3f}秒")

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
    parser = argparse.ArgumentParser(prog="超音速抢座脚本 v2.0")
    parser.add_argument("-u", "--user", default=config_path, help="用户配置文件")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="运行模式",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="启用GitHub Action模式",
    )
    parser.add_argument(
        "--slow-mode",
        action="store_true",
        help="启用兼容模式（较慢但更稳定）",
    )
    args = parser.parse_args()
    
    func_dict = {
        "reserve": lambda users, action: main(users, action, not args.slow_mode), 
        "debug": debug, 
        "room": get_roomid
    }
    
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    
    logging.info(f"🚀 加载了 {len(usersdata)} 个用户配置")
    func_dict[args.method](usersdata, args.action)
