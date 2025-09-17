import json
import time
import argparse
import os
import logging
import datetime
import threading
import copy
import random
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, wait

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

from utils import reserve, get_user_credentials

# --- 时间处理辅助函数 ---
def get_now(action=False):
    """根据运行环境获取当前时间（统一为北京时间）"""
    if action:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        return datetime.datetime.now()

def get_current_dayofweek(action=False):
    """根据运行环境获取今天是周几（统一为北京时间）"""
    return get_now(action).strftime("%A")

def get_target_date(action=False):
    """Get target reservation date"""
    target_dt = get_now(action)
    if RESERVE_NEXT_DAY:
        target_dt += datetime.timedelta(days=1)
    return target_dt.strftime("%Y-%m-%d")

# --- 全局配置 (进一步优化的参数) ---
RESERVE_TARGET_TIME = "18:57:00"
ENABLE_SLIDER = True
RESERVE_NEXT_DAY = False
POOL_SIZE = 5  # 进一步减少池大小
PRELOAD_START_SECONDS = 1.5  # 进一步缩短预加载时间
MAX_TOKEN_AGE = 1.0  # 极短的Token有效期
MAX_CAPTCHA_AGE = 2.0  # 极短的验证码有效期
MAX_CONCURRENT_THREADS = 1  # 单线程提交，避免冲突

class ResourcePool:
    """修复了数据格式问题的资源池"""
    def __init__(self, name, base_session, pool_size, target_func):
        self.name = name
        self.base_session = base_session
        self.pool_size = pool_size
        self.queue = Queue()
        self.is_active = False
        self.target_func = target_func
        self.lock = threading.Lock()

    def _preload_worker(self):
        """预加载工作线程"""
        while self.is_active and self.queue.qsize() < self.pool_size:
            try:
                session_copy = copy.deepcopy(self.base_session)
                resource = self.target_func(session_copy)
                if resource:
                    # 确保资源格式正确
                    timestamped_resource = self._add_timestamp(resource)
                    with self.lock:
                        if self.queue.qsize() < self.pool_size:
                            self.queue.put(timestamped_resource)
                            logging.info(f"✅ {self.name}预加载成功，当前池大小: {self.queue.qsize()}")
                else:
                    time.sleep(0.02)
            except Exception as e:
                logging.warning(f"⚠️ {self.name}预加载线程异常: {e}")
                time.sleep(0.1)

    def _add_timestamp(self, resource):
        """为资源添加时间戳，修复格式问题"""
        current_time = time.time()
        return (resource, current_time)

    def _is_fresh(self, resource, max_age):
        """检查资源是否新鲜"""
        if isinstance(resource, tuple) and len(resource) >= 2:
            timestamp = resource[-1]
            return time.time() - timestamp <= max_age
        return False

    def start(self):
        """启动资源池预加载"""
        if self.is_active: 
            return
        logging.info(f"🔄 启动{self.name}池预加载，目标数量: {self.pool_size}")
        self.is_active = True
        # 使用单个线程预加载
        for _ in range(2):
            thread = threading.Thread(target=self._preload_worker, daemon=True)
            thread.start()

    def stop(self):
        """停止资源池预加载"""
        self.is_active = False
        logging.info(f"🛑 {self.name}池停止预加载.")

    def get_fresh_or_generate(self, max_age_seconds=None):
        """获取新鲜资源，如果没有就立即生成"""
        if max_age_seconds is None:
            max_age_seconds = MAX_TOKEN_AGE if self.name == "Token" else MAX_CAPTCHA_AGE

        # 立即生成新资源，不使用可能过期的预加载资源
        logging.info(f"⚡ 为确保新鲜度，立即生成{self.name}...")
        try:
            session_copy = copy.deepcopy(self.base_session)
            fresh_resource = self.target_func(session_copy)
            return fresh_resource
        except Exception as e:
            logging.error(f"💥 生成{self.name}失败: {e}")
            # 如果生成失败，尝试从池中获取
            return self._get_from_pool(max_age_seconds)

    def _get_from_pool(self, max_age_seconds):
        """从池中获取资源作为备用"""
        attempts = 0
        while not self.queue.empty() and attempts < 3:
            try:
                with self.lock:
                    if not self.queue.empty():
                        resource = self.queue.get_nowait()
                        if self._is_fresh(resource, max_age_seconds):
                            return resource[0]  # 返回去掉时间戳的资源
                attempts += 1
            except:
                break
        return None

    def get(self):
        """向后兼容的get方法"""
        return self.get_fresh_or_generate()

def refresh_session_advanced(session, roomid, seatid):
    """高级session刷新，包含多个步骤"""
    try:
        # 1. 刷新主页面
        session.requests.get("https://office.chaoxing.com/", verify=False, timeout=3)
        
        # 2. 刷新座位页面
        seat_url = f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid}"
        response = session.requests.get(seat_url, verify=False, timeout=3)
        
        # 3. 短暂延迟
        time.sleep(0.05)
        
        logging.info("🔄 高级Session刷新成功")
        return True
    except Exception as e:
        logging.warning(f"⚠️ 高级Session刷新失败: {e}")
        return False

def extract_data_safely(data):
    """安全地提取数据，修复格式问题"""
    if isinstance(data, tuple):
        if len(data) == 1:
            return data[0]
        elif len(data) >= 2:
            return data[0], data[1]
    return data

def lightning_submit_worker(base_session, times, roomid, seatid, captcha_pool, token_pool, action):
    """完全修复的闪电突袭工作线程"""
    # 使用独立的session副本
    session_copy = copy.deepcopy(base_session)
    
    try:
        # 立即获取最新的资源
        captcha_raw = captcha_pool.get_fresh_or_generate()
        token_raw = token_pool.get_fresh_or_generate()
        
        if not captcha_raw or not token_raw:
            logging.error(f"💥 (线程 {times[0]}-{times[1]}) 无法获取有效资源")
            return False

        # 安全地提取数据
        captcha = extract_data_safely(captcha_raw)
        token_data = extract_data_safely(token_raw)
        
        # 确保captcha是字符串
        if isinstance(captcha, tuple):
            captcha = captcha[0]
        
        # 确保token_data格式正确
        if isinstance(token_data, tuple) and len(token_data) >= 2:
            token, value = token_data[0], token_data[1]
        else:
            token = token_data
            value = "1"
        
        # 记录提交参数
        submit_params = {
            'roomId': str(roomid), 
            'startTime': str(times[0]), 
            'endTime': str(times[1]), 
            'day': get_target_date(action), 
            'seatNum': str(seatid), 
            'captcha': str(captcha), 
            'token': str(token), 
            'type': '1', 
            'verifyData': str(value)
        }
        logging.info(f"submit parameter {submit_params}")

        # 提交前微延迟
        time.sleep(0.001)
        
        # 执行提交
        start_time = time.time()
        success = session_copy.get_submit(
            session_copy.submit_url, 
            times=times, 
            token=token, 
            roomid=roomid,
            seatid=seatid, 
            captcha=captcha, 
            action=action, 
            value=value,
        )
        elapsed_time = time.time() - start_time
        
        if success:
            logging.info(f"✅ 提交成功! 耗时: {elapsed_time:.3f}s")
            return True
        else:
            logging.warning(f"❌ 提交失败，耗时: {elapsed_time:.3f}s")
            return False
            
    except Exception as e:
        logging.error(f"💥 (线程 {times[0]}-{times[1]}) 提交异常: {e}")
        return False

def start_lightning_reservation(user_info, base_session, captcha_pool, token_pool, action):
    """修复的闪电预约函数 - 序列化提交避免冲突"""
    username = user_info['username']
    times = user_info['times']
    roomid = user_info['roomid']
    seatid = user_info['seatid']
    
    # 高级session刷新
    refresh_session_advanced(base_session, roomid, seatid[0])
    
    logging.info(f"🚀 用户 {username} 准备序列化提交!")
    time_slots = times if isinstance(times[0], list) else [times]
    
    # 序列化提交，避免并发冲突
    successful_slots = []
    for i, slot in enumerate(time_slots):
        try:
            logging.info(f"📍 正在提交第 {i+1}/{len(time_slots)} 个时间段: {slot}")
            
            # 每次提交前添加小延迟
            if i > 0:
                time.sleep(0.05)
            
            success = lightning_submit_worker(
                base_session, slot, roomid, seatid[0], 
                captcha_pool, token_pool, action
            )
            
            if success:
                successful_slots.append(slot)
                logging.info(f"🎉 时间段 {slot} 预约成功!")
                # 成功后可以选择继续或停止
                # break  # 如果只要一个成功就够了，可以取消注释
            else:
                logging.warning(f"😞 时间段 {slot} 预约失败")
                
        except Exception as e:
            logging.error(f"处理时间段 {slot} 时出错: {e}")
    
    if successful_slots:
        logging.info(f"🎉 胜利! 用户 {username} 成功预约 {len(successful_slots)} 个时间段: {successful_slots}")
    else:
        logging.warning(f"😞 任务结束. 用户 {username} 所有时间段预约均失败.")

def pre_login_user(user, usernames, passwords, action, index):
    """预登录用户"""
    username, password, _, roomid, seatid, daysofweek = user.values()
    
    if action:
        username, password = usernames.split(",")[index], passwords.split(",")[index]
    
    user['username'] = username

    if get_current_dayofweek(action) not in daysofweek:
        logging.info(f"用户 {username}: 今天不是预约日, 跳过.")
        return None

    logging.info(f"User {username}: 提前登录中...")
    s = reserve(enable_slider=ENABLE_SLIDER, reserve_next_day=RESERVE_NEXT_DAY)
    s.get_login_status()
    login_success, msg = s.login(username, password)
    
    if login_success:
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        try:
            logging.info(f"🔥 用户 {username}: 开始Session预热...")
            s.requests.get(f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}", verify=False)
            logging.info(f"✅ 用户 {username}: Session预热完成.")
        except Exception as e:
            logging.warning(f"⚠️ 用户 {username} Session预热失败: {e}")
        return s
    else:
        logging.error(f"❌ 用户 {username} 登录失败: {msg}")
        return None

def main(users, action=False):
    logging.info(f"程序启动 (action={'on' if action else 'off'})")
    logging.info(f"优化配置 - 池大小: {POOL_SIZE}, 预加载时间: {PRELOAD_START_SECONDS}秒")
    logging.info(f"资源有效期 - Token: {MAX_TOKEN_AGE}秒, 验证码: {MAX_CAPTCHA_AGE}秒")
    
    usernames, passwords = get_user_credentials(action) if action else (None, None)

    # 预登录所有用户
    user_sessions = [pre_login_user(u, usernames, passwords, action, i) for i, u in enumerate(users)]

    # 为每个成功登录的用户创建资源池
    captcha_pools = []
    token_pools = []
    for i, session in enumerate(user_sessions):
        if session:
            # 定义获取资源的函数
            get_captcha_func = lambda s: s.resolve_captcha()
            get_token_func = lambda s: s._get_page_token(
                s.url.format(users[i]['roomid'], users[i]['seatid'][0]), require_value=True
            )
            
            captcha_pool = ResourcePool("验证码", session, POOL_SIZE, get_captcha_func)
            token_pool = ResourcePool("Token", session, POOL_SIZE, get_token_func)
            
            captcha_pools.append(captcha_pool)
            token_pools.append(token_pool)
        else:
            captcha_pools.append(None)
            token_pools.append(None)

    # 时间计算与等待
    now_dt = get_now(action)
    target_dt = now_dt.replace(
        hour=int(RESERVE_TARGET_TIME.split(":")[0]),
        minute=int(RESERVE_TARGET_TIME.split(":")[1]),
        second=int(RESERVE_TARGET_TIME.split(":")[2]),
        microsecond=0
    )
    if target_dt <= now_dt:
        target_dt += datetime.timedelta(days=1)
    
    preload_start_dt = target_dt - datetime.timedelta(seconds=PRELOAD_START_SECONDS)
    
    # 等待预加载开始时间
    now_dt = get_now(action)
    if now_dt < preload_start_dt:
        wait_seconds = (preload_start_dt - now_dt).total_seconds()
        if wait_seconds > 0:
            logging.info(f"将在 {wait_seconds:.1f} 秒后开始预加载...")
            time.sleep(wait_seconds)
    
    # 启动所有用户的资源池
    active_pools = [pool for pool in captcha_pools + token_pools if pool]
    for pool in active_pools:
        pool.start()
    
    # 等待到达目标时间
    now_dt = get_now(action)
    if now_dt < target_dt:
        wait_seconds = (target_dt - now_dt).total_seconds()
        if wait_seconds > 0:
            logging.info(f"距离目标时间 {RESERVE_TARGET_TIME} 还剩 {wait_seconds:.1f} 秒, 精准等待中...")
            time.sleep(wait_seconds)
    
    # 精确等待到目标时间
    while get_now(action) < target_dt:
        time.sleep(0.0001)

    logging.info(f"到达目标时间 {RESERVE_TARGET_TIME}, 开始精确打击!")

    # 序列化处理每个用户（避免并发冲突）
    for i, session in enumerate(user_sessions):
        if session and captcha_pools[i] and token_pools[i]:
            start_lightning_reservation(users[i], session, captcha_pools[i], token_pools[i], action)
            # 用户之间添加小延迟
            time.sleep(0.01)

    # 停止所有资源池
    for pool in active_pools:
        pool.stop()

    logging.info("🏁 所有预约任务已完成!")

if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument("-m", "--method", default="reserve", choices=["reserve"], help="run method")
    parser.add_argument("-a", "--action", action="store_true", help="enable for github action")
    args = parser.parse_args()
    
    with open(args.user, "r+") as data:
        config = json.load(data)
        usersdata = config["reserve"]
        if "target_time" in config:
            RESERVE_TARGET_TIME = config["target_time"]
            logging.info(f"已从config.json加载预约时间: {RESERVE_TARGET_TIME}")
        
    main(usersdata, args.action)
