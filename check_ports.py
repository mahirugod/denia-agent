import socket

def check(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(('127.0.0.1', port))
        return 'LISTENING'
    except Exception:
        return 'FREE'
    finally:
        s.close()

print(f"3000 {check(3000)}")
print(f"6099 {check(6099)}")
