
f = open('tests/test_etl_engine.py', 'rb')
data = f.read()
f.close()
target = bytes([0x22, 0x5C, 0x78, 0x38, 0x39, 0x50, 0x4E, 0x47, 0x2E, 0x2E, 0x2E, 0x22])
replacement = b'minimal_png()'
data = data.replace(target, replacement)
f = open('tests/test_etl_engine.py', 'wb')
f.write(data)
f.close()
print('Fixed')
