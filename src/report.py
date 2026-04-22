# -*- coding: utf-8 -*-
import os
import sys
import json
import datetime
import warnings
import requests

# 修正 urllib3 在 Python 3.12 下引发的 SNI 丢失问题
try:
    from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_
    ssl_.HAS_SNI = True
except Exception:
    pass

import socket
# 强制使用 IPv4 避免 IPv6 黑洞
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_res = [r for r in res if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else res
socket.getaddrinfo = _getaddrinfo_ipv4_only

warnings.filterwarnings("ignore")

try:
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest
except ImportError:
    sys.exit(1)

CONFIG_FILE = '/opt/scripts/config.json'

def load_config():
    if not os.path.exists(CONFIG_FILE):
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

# ---------- PushPlus 微信推送 ----------
def send_pushplus_report(pushplus_conf, title, content):
    """通过 PushPlus 发送日报"""
    if not pushplus_conf.get('token'):
        print("PushPlus token 未配置，跳过发送")
        return
    try:
        url = "http://www.pushplus.plus/send"
        data = {
            "token": pushplus_conf['token'],
            "title": title,
            "content": content,
            "template": pushplus_conf.get('template', 'html'),
            "channel": pushplus_conf.get('channel', 'wechat')
        }
        headers = {"Content-Type": "application/json"}
        response = requests.post(url, json=data, headers=headers, timeout=10)
        result = response.json()
        if result.get('code') == 200:
            print(f"PushPlus 日报发送成功")
        else:
            print(f"PushPlus 发送失败: {result.get('msg', '未知错误')}")
    except Exception as e:
        print(f"PushPlus 发送异常: {e}")

def do_common_request(client, domain, version, action, params=None, method='POST', timeout=30, retries=3):
    for attempt in range(1, retries + 1):
        try:
            request = CommonRequest()
            request.set_domain(domain)
            request.set_version(version)
            request.set_action_name(action)
            request.set_method(method)
            request.set_protocol_type('https')
            request.set_connect_timeout(5000)   # 连接 5 秒内必须成功，避免黑洞 IP 卡死
            request.set_read_timeout(15000)      # 读取 15 秒
            if params:
                for k, v in params.items():
                    request.add_query_param(k, v)
            response = client.do_action_with_exception(request)
            return json.loads(response.decode('utf-8'))
        except Exception as e:
            if attempt < retries:
                import time
                time.sleep(2 * attempt)
                continue
            return None

def main():
    config = load_config()
    users = config.get('users', [])
    pushplus_conf = config.get('pushplus', {})
    
    report_lines = []
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    report_lines.append(f"<h2>📊 阿里云多账号 - 每日财报</h2>")
    report_lines.append(f"<p>📅 日期: {today}</p>")
    report_lines.append("<hr>")

    for user in users:
        try:
            target_id = user.get('instance_id', '').strip()
            target_region = user.get('region', '').strip()
            resgroup = user.get('resgroup', '').strip()

            # [名字显示修复] 优先使用备注，没有则用ID，再没有则用Unknown
            user_name = user.get('name', '').strip()
            if not user_name:
                user_name = target_id if target_id else "Unknown_Device"
            
            client = AcsClient(user['ak'].strip(), user['sk'].strip(), target_region)
            
            # 1. CDT 流量
            traffic_data = do_common_request(AcsClient(user['ak'].strip(), user['sk'].strip(), 'cn-hangzhou'), 'cdt.aliyuncs.com', '2021-08-13', 'ListCdtInternetTraffic')
            traffic_gb = -1  # -1 表示查询失败
            if traffic_data:
                traffic_gb = sum(d.get('Traffic', 0) for d in traffic_data.get('TrafficDetails', [])) / (1024**3)

            # 2. BSS 账单 (兼容国际站/国内站: 优先 DescribeInstanceBill，失败回退 QueryBillOverview)
            bill_amount = -1
            bill_currency = 'USD'

            # 尝试1: DescribeInstanceBill (精确到实例)
            bill_params = {
                'BillingCycle': datetime.datetime.now().strftime("%Y-%m"),
                'InstanceID': target_id
            }
            bill_data = do_common_request(client, 'business.aliyuncs.com', '2017-12-14', 'DescribeInstanceBill', bill_params, retries=1)
            if bill_data and bill_data.get('Success'):
                items = bill_data.get('Data', {}).get('Items', [])
                if items:
                    bill_amount = sum(float(item.get('PretaxAmount', 0)) for item in items)
                    bill_currency = items[0].get('Currency', 'USD')

            # 尝试2: 回退到 QueryBillOverview (国际站兼容)
            if bill_amount == -1:
                bill_params2 = {'BillingCycle': datetime.datetime.now().strftime("%Y-%m")}
                bill_endpoint = user.get('bill_endpoint', 'business.ap-southeast-1.aliyuncs.com')
                bill_data2 = do_common_request(client, bill_endpoint, '2017-12-14', 'QueryBillOverview', bill_params2)
                if bill_data2:
                    items2 = bill_data2.get('Data', {}).get('Items', {}).get('Item', [])
                    bill_amount = sum(float(item.get('PretaxAmount', 0)) for item in items2)
                    if items2:
                        bill_currency = items2[0].get('Currency', 'USD')

            # 3. ECS 状态
            ecs_params = {'PageSize': 50, 'RegionId': target_region}
            if resgroup:
                ecs_params['ResourceGroupId'] = resgroup
            ecs_data = do_common_request(client, 'ecs.aliyuncs.com', '2014-05-26', 'DescribeInstances', ecs_params)
            
            status, ip, spec = "NotFound", "N/A", "N/A"
            
            if ecs_data and 'Instances' in ecs_data:
                for inst in ecs_data['Instances'].get('Instance', []):
                    if inst['InstanceId'] == target_id:
                        status = inst.get('Status', 'Unknown')
                        # IP
                        pub = inst.get('PublicIpAddress', {}).get('IpAddress', [])
                        eip = inst.get('EipAddress', {}).get('IpAddress', "")
                        ip = eip if eip else (pub[0] if pub else "无公网IP")
                        
                        # Spec (0.5G 内存修复)
                        cpu = inst.get('Cpu', 0)
                        mem_mb = inst.get('Memory', 0)
                        if mem_mb > 0 and mem_mb % 1024 == 0:
                            mem_str = f"{int(mem_mb/1024)}"
                        else:
                            mem_str = f"{mem_mb/1024:.1f}"
                        
                        spec = f"{cpu}C{mem_str}G"
                        break 

            # 4. 判定
            quota = user.get('traffic_limit', 180)
            bill_limit = user.get('bill_threshold', 1.0)
            
            if traffic_gb >= 0:
                percent = (traffic_gb / quota) * 100
                traffic_str = f"{traffic_gb:.2f} GB ({percent:.1f}%)"
            else:
                percent = 0
                traffic_str = "⚠️ 查询失败"
            
            bill_str = f"${bill_amount:.2f}" if bill_amount != -1 else "Fail"
            if bill_amount != -1 and bill_currency == 'CNY':
                bill_str = f"¥{bill_amount:.2f}"
                bill_limit = bill_limit * 7.0  # USD 阈值换算为 CNY
            elif bill_amount != -1:
                # 覆盖货币符号（支持根据配置动态显示）
                currency_symbol = user.get('currency', '$')
                bill_str = f"{currency_symbol}{bill_amount:.2f}"

            status_icon = "✅"
            if traffic_gb >= 0 and traffic_gb > quota: status_icon = "⚠️ 流量超标"
            if bill_amount > bill_limit: status_icon = "💸 扣费预警"
            if traffic_gb < 0: status_icon = "⚠️ 流量查询异常"
            
            run_icon = "🟢"
            if status == "Running": run_icon = "🟢"
            elif status == "Stopped": run_icon = "⚫"
            elif status == "NotFound": run_icon = "❓"
            else: run_icon = "🟡"

            user_report = f"""
<div style="margin-bottom: 20px; padding: 15px; border: 1px solid #ddd; border-radius: 8px;">
    <h3>👤 {user_name} ({spec})</h3>
    <ul style="list-style: none; padding-left: 0;">
        <li>🖥️ 状态: {run_icon} {status}</li>
        <li>🌐 IP: <code>{ip}</code></li>
        <li>📉 流量: {traffic_str}</li>
        <li>💰 账单: <b>{bill_str}</b></li>
        <li>📝 评价: {status_icon}</li>
    </ul>
</div>
"""
            report_lines.append(user_report)

        except Exception as e:
            report_lines.append(f"<div style='color: red;'>❌ {user.get('name', 'Unknown')} Error: {str(e)}</div>")

    final_content = "\n".join(report_lines)
    send_pushplus_report(pushplus_conf, f"📊 阿里云每日财报 - {today}", final_content)

if __name__ == "__main__":
    main()
