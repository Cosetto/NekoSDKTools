import os
import json
import chardet
import sqlite3
import sys

def help():
	print('usage:src.py [-d|-i]')
	print('\t-d\texport text')
	print('\t-i\timport text')

def _init_():
    file_all = os.listdir()
    if 'intermediate_file' not in file_all:
        os.mkdir('intermediate_file')
    if 'output' not in file_all:
        os.mkdir('output')
    if 'input' not in file_all:
        os.mkdir('input')

def to_bytes(a: int, _len: int) -> int:
    return int.to_bytes(a, _len, byteorder='little')


def from_bytes(a: bytes):
    return int.from_bytes(a, byteorder='little')
    
def open_json(path):
    with open(path, 'r', encoding='utf8') as f:
        return json.loads(f.read())

def save_json(path, data):
    with open(path, 'w', encoding='utf8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))


def save_file(path, data):
    with open(path, 'w', encoding='utf8') as f:
        f.write(data)


def open_file_b(path):
    with open(path, 'rb') as f:
        return f.read()


def save_file_b(path, data):
    with open(path, 'wb') as f:
        f.write(data)

class NEKOSDK():
    def extract():
        file_all = os.listdir('input')
        ans = []

        for f in file_all:
            data = open_file_b(f'input/{f}')
            cnt = len(ans)
            p = 0
            while p < len(data):
                if data[p:p+0xe] == b'\x5B\x83\x65\x83\x4C\x83\x58\x83\x67\x95\x5C\x8E\xA6\x5D':
                    p -= 4
                    _len = from_bytes(data[p:p+4])
                    p += (_len + 4)
                    _len1 = from_bytes(data[p:p+4])
                    p += 4
                    _str = data[p:p+_len1-1].decode('cp932')
                    if _str:
                        ans.append(_str)
                    p += _len1
                    _len2 = from_bytes(data[p:p+4])
                    p += 4
                    ans.append(data[p:p+_len2-1].decode('cp932'))
                    p += _len2
                elif data[p:p+7] == b"\x91\x49\x91\xF0\x8E\x88\x0d":
                    p -= 4
                    _len = from_bytes(data[p:p+4])
                    p+=12
                    ans.append(data[p:p+_len-9].decode('cp932'))
                    p+=_len - 8
                p += 1
            # print(f, len(ans)-cnt)
        save_file('intermediate_file/jp_all.txt', '\n'.join(ans))
        if os.path.exists('intermediate_file/script.json'):
            script = open_json('intermediate_file/script.json')
        else:
            script = dict()
        for i in ans:
            if i not in script:
                script[i] = ''
        save_json('intermediate_file/script.json', script)

    def insert():
        file_all = os.listdir('input')
        failed = []
        cnt = 0
        script = open_json('intermediate_file/script.json')
        for f in file_all:
            data = open_file_b(f'input/{f}')
            data = bytearray(data)
            p = 0
            while p < len(data):
                if data[p:p+0xe] == b'\x5B\x83\x65\x83\x4C\x83\x58\x83\x67\x95\x5C\x8E\xA6\x5D':
                    p -= 4
                    _len = from_bytes(data[p:p+4])
                    p += (_len + 4)

                    _len1 = from_bytes(data[p:p+4])

                    _str = data[p+4:p+3+_len1]
                    _str = _str.decode('cp932')
                    if _str in script and script[_str]:
                        _str = script[_str]
                        _str = _str.replace('\t','')
                        _str = _str.encode('cp932')
                        data[p:p+4] = to_bytes(len(_str)+1, 4)
                        data[p+4:p+3+_len1] = _str
                        cnt += 1
                        p += (len(_str)+1)
                    else:
                        failed.append(_str)
                        p += _len1
                    p += 4

                    _len2 = from_bytes(data[p:p+4])

                    _str = data[p+4:p+3+_len2]
                    _str = _str.decode('cp932')
                    if _str in script and script[_str]:
                        _str = script[_str]
                        _str = _str.encode('cp932')
                        data[p:p+4] = to_bytes(len(_str)+1, 4)
                        data[p+4:p+3+_len2] = _str
                        cnt += 1
                        p += (len(_str)+1)
                    else:
                        failed.append(_str)
                        p += _len2
                    p += 4
                elif data[p:p+7] == b"\x91\x49\x91\xF0\x8E\x88\x0d":
                    p -= 4
                    len_p = p
                    _len = from_bytes(data[p:p+4])
                    p+=12
                    _str = data[p:p+_len-9].decode('cp932')
                    if _str in script and script[_str]:
                        _str = script[_str]
                        _str = _str.encode('cp932')
                        data[len_p:len_p+4] = to_bytes(8+len(_str), 4)
                        cnt += 1
                        data[p:p+_len-9] = _str
                        p += (len(_str)+1)
                    else:
                        failed.append(_str)
                        p+=_len - 8
                p += 1
            save_file_b(f'output/{f}', data)
        print('Fail：', cnt, 'Success：', len(failed))
        save_file('intermediate_file/failed.txt', '\n'.join(failed))
        
        
def main():
	if len(sys.argv) != 2:
		help()
	else:
		if sys.argv[1] == '-d':
			NEKOSDK.extract()
		elif sys.argv[1] == '-i':
			NEKOSDK.insert()
		else:
			help()
main()