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

# --- 全局配置 (优化后的参数) ---
RESERVE_TARGET_TIME = "18:50:00"
ENABLE_SLIDER = True
RESERVE_NEXT_DAY = False
POOL_SIZE = 8  # 减少池大小，避免服务器过载
PRELOAD_START_SECONDS = 3  # 减少预加载时间，防止token过期
MAX_TOKEN_AGE = 2  # Token最大存活时间（秒）
MAX_CAPTCHA_AGE = 4  # 验证码最大存活时间（秒）
MAX_CONCURRENT_THREADS = 2  # 减少并发线程数

class ResourcePool:
    """优化的资源池，支持新鲜度检查和时间戳"""
    def __init__(self, name, base_session, pool_size, target_func):
        self.name = name
        self.base_session = base_session
        self.pool_size = pool_size
        self.queue = Queue()
        self.is_active = False
        self.target_func = target_func
        self.lock = threading.Lock()

    def _preload_worker(self):
        """预加载工作线程，添加时间戳"""
        while self.is_active and self.queue.qsize() < self.pool_size:
            try:
                session_copy = copy.deepcopy(self.base_session)
                resource = self.target_func(session_copy)
                if resource:
                    # 为资源添加时间戳
                    timestamped_resource = self._add_timestamp(resource)
                    with self.lock:
                        if self.queue.qsize() < self.pool_size:
                            self.queue.put(timestamped_resource)
                            logging.info(f"✅ {self.name}预加载成功，当前池大小: {self.queue.qsize()}")
                else:
                    time.sleep(0.05 + random.uniform(0, 0.05))  # 添加随机抖动
            except Exception as e:
                logging.warning(f"⚠️ {self.name}预加载线程异常: {e}")
                time.sleep(0.2 + random.uniform(0, 0.1))

    def _add_timestamp(self, resource):
        """为资源添加时间戳"""
        current_time = time.time()
        if isinstance(resource, tuple):
            return (*resource, current_time)
        else:
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
        # 使用适量线程并行预加载
        thread_count = min(3, self.pool_size // 2 + 1)
        for _ in range(thread_count):
            thread = threading.Thread(target=self._preload_worker, daemon=True)
            thread.start()

    def stop(self):
        """停止资源池预加载"""
        self.is_active = False
        logging.info(f"🛑 {self.name}池停止预加载.")

    def get_fresh(self, max_age_seconds=None):
        """获取新鲜资源，如果过期则重新生成"""
        if max_age_seconds is None:
            max_age_seconds = MAX_TOKEN_AGE if self.name == "Token" else MAX_CAPTCHA_AGE

        # 尝试从池中获取新鲜资源
        attempts = 0
        while not self.queue.empty() and attempts < 5:
            try:
                with self.lock:
                    if not self.queue.empty():
                        resource = self.queue.get_nowait()
                        if self._is_fresh(resource, max_age_seconds):
                            # 返回去掉时间戳的资源
                            return resource[:-1] if len(resource) > 1 else resource[0]
                attempts += 1
            except:
                break

        # 如果没有新鲜资源，立即生成新的
        logging.warning(f"⚠️ {self.name}池无新鲜资源，正在紧急生成...")
        try:
            session_copy = copy.deepcopy(self.base_session)
            return self.target_func(session_copy)
        except Exception as e:
            logging.error(f"💥 紧急生成{self.name}失败: {e}")
            return None

    def get(self):
        """向后兼容的get方法"""
        return self.get_fresh()

def refresh_session(session, roomid, seatid):
    """在关键操作前刷新session"""
    try:
        session.requests.get(
            f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid}",
            verify=False,
            timeout=3
        )
        logging.info("🔄 Session刷新成功")
        return True
    except Exception as e:
        logging.warning(f"⚠️ Session刷新失败: {e}")
        return False

def lightning_submit_worker(base_session, times, roomid, seatid, captcha_pool, token_pool, action):
    """优化的闪电突袭工作线程"""
    session_copy = copy.deepcopy(base_session)
    
    try:
        # 在提交前获取最新鲜的资源
        captcha = captcha_pool.get_fresh(max_age_seconds=MAX_CAPTCHA_AGE)
        token_data = token_pool.get_fresh(max_age_seconds=MAX_TOKEN_AGE)
        
        if not captcha or not token_data:
            logging.error(f"💥 (线程 {times[0]}-{times[1]}) 无法获取有效的验证码或Token")
            return False

        if isinstance(token_data, tuple) and len(token_data) >= 2:
            token, value = token_data[0], token_data[1]
        else:
            token, value = token_data, "1"

        # 记录提交参数（用于调试）
        submit_params = {
            'roomId': roomid, 
            'startTime': times[0], 
            'endTime': times[1], 
            'day': get_target_date(action), 
            'seatNum': seatid, 
            'captcha': captcha, 
            'token': token, 
            'type': '1', 
            'verifyData': value
        }
        logging.info(f"submit parameter {submit_params}")

        # 执行提交，添加重试机制
        success = submit_with_retry(session_copy, times, token, roomid, seatid, captcha, action, value)
        return success
        
    except Exception as e:
        logging.error(f"💥 (线程 {times[0]}-{times[1]}) 提交时异常: {e}")
        return False

def submit_with_retry(session, times, token, roomid, seatid, captcha, action, value, max_retries=1):
    """带重试机制的提交函数"""
    for attempt in range(max_retries + 1):
        try:
            start_time = time.time()
            success = session.get_submit(
                session.submit_url, times=times, token=token, roomid=roomid,
                seatid=seatid, captcha=captcha, action=action, value=value,
            )
            elapsed_time = time.time() - start_time
            
            if success:
                logging.info(f"✅ 提交成功! 耗时: {elapsed_time:.3f}s")
                return True
            elif attempt < max_retries:
                logging.info(f"🔄 第 {attempt + 1} 次提交失败，{0.05}秒后重试...")
                time.sleep(0.05)
                
        except Exception as e:
            if attempt < max_retries:
                logging.warning(f"⚠️ 第 {attempt + 1} 次提交异常，准备重试: {e}")
                time.sleep(0.05)
            else:
                logging.error(f"💥 所有重试均失败: {e}")
    
    return False

def start_lightning_reservation(user_info, base_session, captcha_pool, token_pool, action):
    """优化的闪电预约函数"""
    username = user_info['username']
    times = user_info['times']
    roomid = user_info['roomid']
    seatid = user_info['seatid']
    
    # 在提交前刷新session
    refresh_session(base_session, roomid, seatid[0])
    
    logging.info(f"🚀 用户 {username} 所有线程准备就绪, 开始闪电突袭!")
    time_slots = times if isinstance(times[0], list) else [times]
    
    # 限制并发线程数
    max_workers = min(len(time_slots), MAX_CONCURRENT_THREADS)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, slot in enumerate(time_slots):
            try:
                # 添加小的延迟避免同时提交
                if i > 0:
                    time.sleep(0.01)
                    
                future = executor.submit(
                    lightning_submit_worker, 
                    base_session, slot, roomid, seatid[0], 
                    captcha_pool, token_pool, action
                )
                futures.append(future)
            except Exception as e:
                logging.error(f"为时间段 {slot} 分配任务时出错: {e}")

        # 等待所有任务完成
        wait(futures)

        # 统计结果
        successful_slots = []
        for i, future in enumerate(futures):
            if future.done():
                try:
                    if future.result():
                        successful_slots.append(time_slots[i])
                except Exception as e:
                    logging.error(f"获取线程结果时出错: {e}")
        
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

def log_pool_status(captcha_pools, token_pools):
    """记录资源池状态（调试用）"""
    for i, (cp, tp) in enumerate(zip(captcha_pools, token_pools)):
        if cp and tp:
            logging.info(f"用户 {i+1} - 验证码池: {cp.queue.qsize()}, Token池: {tp.queue.qsize()}")

def main(users, action=False):
    logging.info(f"程序启动 (action={'on' if action else 'off'})")
    logging.info(f"配置参数 - 池大小: {POOL_SIZE}, 预加载时间: {PRELOAD_START_SECONDS}秒, Token有效期: {MAX_TOKEN_AGE}秒")
    
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
            # 在等待期间监控池状态
            sleep_interval = 0.5
            slept_time = 0
            while slept_time < wait_seconds:
                time.sleep(min(sleep_interval, wait_seconds - slept_time))
                slept_time += sleep_interval
                if slept_time % 2 < sleep_interval:  # 每2秒记录一次状态
                    log_pool_status(captcha_pools, token_pools)
    
    # 精确等待到目标时间
    while get_now(action) < target_dt:
        time.sleep(0.001)

    logging.info(f"到达目标时间 {RESERVE_TARGET_TIME}, 所有用户总攻开始!")

    # 对每个用户发起总攻
    reservation_threads = []
    for i, session in enumerate(user_sessions):
        if session and captcha_pools[i] and token_pools[i]:
            # 为每个用户创建单独的线程，避免阻塞
            thread = threading.Thread(
                target=start_lightning_reservation,
                args=(users[i], session, captcha_pools[i], token_pools[i], action),
                daemon=True
            )
            reservation_threads.append(thread)
            thread.start()
            
            # 添加小延迟避免同时开始
            time.sleep(0.01)
    
    # 等待所有预约线程完成
    for thread in reservation_threads:
        thread.join(timeout=10)  # 最多等待10秒

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
