import sys
import requests

print(f"Python版本: {sys.version}")
print(f"Python路径: {sys.executable}")
print(f"Requests版本: {requests.__version__}")

try:
    response = requests.get("https://httpbin.org/get", timeout=5)
    print(f"网络测试成功: {response.status_code}")
except Exception as e:
    print(f"网络测试失败: {e}")

print("环境测试完成！")

