# ===================== 第一步：全局拦截 Nominatim 请求（核心新增）=====================
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from unittest.mock import Mock

# 定义拦截适配器：所有指向 nominatim.openstreetmap.org 的请求直接返回模拟结果
class BlockNominatimAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        if "nominatim.openstreetmap.org" in request.url:
            # 模拟成功响应，返回空地址或固定「中国」，避免超时
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json = lambda: {"display_name": "中国"}
            mock_response.text = '{"display_name": "中国"}'
            return mock_response
        # 其他请求正常发送（如高德 API）
        return super().send(request, **kwargs)

# 给 requests 全局挂载拦截适配器，彻底阻断 Nominatim 请求
session = requests.Session()
adapter = BlockNominatimAdapter(
    max_retries=Retry(total=0, connect=0, read=0, redirect=0)  # 禁用重试，避免无效请求
)
session.mount("https://", adapter)
session.mount("http://", adapter)
# 替换 requests.get/post 为拦截后的 session 方法，确保所有请求都被拦截
requests.get = session.get
requests.post = session.post

# ===================== 第二步：高德 API 配置（保留原有逻辑）=====================
import sys
import json
import time
from datetime import datetime
import pytz

try:
    from rich import print
except Exception:
    pass
from generator import Generator
from stravalib.client import Client
from stravalib.exc import RateLimitExceeded

# 你的高德 Web 服务 API Key
AMAP_API_KEY = "f32107837ead6cc930a9ea898de2844c"
AMAP_REVERSE_GEO_URL = "https://restapi.amap.com/v3/geocode/regeo"

def amap_reverse_geocode(lat, lon):
    """高德逆地理编码：经纬度转真实地址"""
    try:
        params = {
            "location": f"{lon},{lat}",
            "key": AMAP_API_KEY,
            "coordtype": "wgs84ll",
            "extensions": "base",
            "batch": "false"
        }
        response = requests.get(AMAP_REVERSE_GEO_URL, params=params, timeout=5)
        response.raise_for_status()
        result = response.json()
        
        if result.get("status") == "1" and "regeocode" in result:
            return result["regeocode"].get("formatted_address", "中国")
        else:
            print(f"高德API返回异常: {result.get('info', '未知错误')}")
            return "中国"
    except Exception as e:
        print(f"高德逆地理编码失败(lat={lat}, lon={lon}): {str(e)}")
        return "中国"

# ===================== 第三步：Mock geopy（保留原有逻辑）=====================
class MockGeoLocator:
    def reverse(self, location, *args, **kwargs):
        lat, lon = location
        address = amap_reverse_geocode(lat, lon)
        mock = Mock()
        mock.address = address
        return mock

sys.modules['geopy'] = Mock()
sys.modules['geopy.geocoders'] = Mock()
sys.modules['geopy.geocoders.Nominatim'] = MockGeoLocator

# ===================== 第四步：原有业务逻辑（完全保留）=====================
def adjust_time(time, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    return time + tc_offset

def adjust_time_to_utc(time, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    return time - tc_offset

def adjust_timestamp_to_utc(timestamp, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    delta = int(tc_offset.total_seconds())
    return int(timestamp) - delta

def to_date(ts):
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        ts_fmts = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"]
        for ts_fmt in ts_fmts:
            try:
                return datetime.strptime(ts, ts_fmt)
            except ValueError:
                pass
        raise ValueError(f"cannot parse timestamp {ts} into date")

def make_activities_file(
    sql_file, data_dir, json_file, file_suffix="gpx", activity_title_dict={}
):
    generator = Generator(sql_file)
    generator.sync_from_data_dir(
        data_dir, file_suffix=file_suffix, activity_title_dict=activity_title_dict
    )
    activities_list = generator.load()
    with open(json_file, "w") as f:
        json.dump(activities_list, f)

def make_strava_client(client_id, client_secret, refresh_token):
    client = Client()
    refresh_response = client.refresh_access_token(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )
    client.access_token = refresh_response["access_token"]
    return client

def get_strava_last_time(client, is_milliseconds=True):
    try:
        activity = None
        activities = client.get_activities(limit=10)
        activities = list(activities)
        activities.sort(key=lambda x: x.start_date, reverse=True)
        for a in activities:
            if a.type == "Run":
                activity = a
                break
        else:
            return 0
        end_date = activity.start_date + activity.elapsed_time
        last_time = int(datetime.timestamp(end_date))
        if is_milliseconds:
            last_time = last_time * 1000
        return last_time
    except Exception as e:
        print(f"Something wrong to get last time err: {str(e)}")
        return 0

def upload_file_to_strava(client, file_name, data_type, force_to_run=True):
    with open(file_name, "rb") as f:
        try:
            if force_to_run:
                r = client.upload_activity(
                    activity_file=f, data_type=data_type, activity_type="run"
                )
            else:
                r = client.upload_activity(activity_file=f, data_type=data_type)
        except RateLimitExceeded as e:
            timeout = e.timeout
            print(f"Strava API Rate Limit Exceeded. Retry after {timeout} seconds")
            time.sleep(timeout)
            if force_to_run:
                r = client.upload_activity(
                    activity_file=f, data_type=data_type, activity_type="run"
                )
            else:
                r = client.upload_activity(activity_file=f, data_type=data_type)
        print(
            f"Uploading {data_type} file: {file_name} to strava, upload_id: {r.upload_id}."
        )
