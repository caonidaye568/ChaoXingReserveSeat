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
SLEEPTIME = 0.01  # 大幅减少等待时间
ENDTIME = "07:01:00"
ENABLE_SLIDER = True
MAX_ATTEMPT = 3  # 减少重试次数
RESERVE_NEXT_DAY = False

# 并发相关参数
MAX_WORKERS = 8  # 最大并发线程数
success_lock = Lock()

def reserve_single_user(user_data):
    """单用户预约函数，用于并发执行"""
    index, user, username, password, action, current_dayofweek = user_data
    
    user_username, user_password, times, roomid, seatid, daysofweek = user.values()
    if action:
        user_username, user_password = username, password
        
    if current_dayofweek not in daysofweek:
        logging.info(f"用户 {index}: 今日无需预约")
        return index, False
        
    start_time = time.time()
    logging.info(f"🚀 用户 {user_username} 开始抢座 时段:{times} 座位:{seatid}")
    
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    
    try:
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

def login_and_reserve_parallel(users, usernames, passwords, action, success_list=None):
    """并发版本的登录和预约函数"""
    logging.info(f"🔥 优化模式启动! SLEEPTIME={SLEEPTIME}, MAX_ATTEMPT={MAX_ATTEMPT}, MAX_WORKERS={MAX_WORKERS}")
    
    if action and len(usernames.split(",")) != len(users):
        raise Exception("用户数量不匹配配置文件数量")
    
    if success_list is None:
        success_list = [False] * len(users)
    
    current_dayofweek = get_current_dayofweek(action)
    
    # 准备并发任务数据
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
    
    # 并发执行
    start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_index = {
            executor.submit(reserve_single_user, task): task[0] 
            for task in tasks
        }
        
        for future in concurrent.futures.as_completed(future_to_index):
            index, suc = future.result()
            with success_lock:
                success_list[index] = suc
    
    total_elapsed = time.time() - start_time
    success_count = sum(success_list)
    logging.info(f"📊 本轮结果: {success_count}/{len(users)} 成功, 总耗时: {total_elapsed:.3f}秒")
    
    return success_list

def login_and_reserve(users, usernames, passwords, action, success_list=None):
    """原有的串行版本，保持向后兼容"""
    logging.info(f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}")
    
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
            success_list[index] = suc
    return success_list

def main(users, action=False, use_parallel=True):
    """主函数，新增并发开关"""
    current_time = get_current_time(action)
    mode_str = "🚀 并发模式" if use_parallel else "🐌 串行模式"
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
    
    # 选择使用并发还是串行版本
    reserve_func = login_and_reserve_parallel if use_parallel else login_and_reserve
    
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
            logging.info(f"🔄 第 {attempt_times}
