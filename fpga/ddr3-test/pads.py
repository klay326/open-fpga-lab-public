import serial,time,sys
s=serial.Serial(sys.argv[1],9600,timeout=1); time.sleep(0.2); s.reset_input_buffer()
def q(c):
    s.write(c); b=s.read(1); return b[0] if b else None
names=['ODT','WE#','CAS#','RAS#','CKE','RESET#','CK']
st=[q(b'?')-0x40 for _ in range(6)]
print('states', st)
s.write(b'#'); time.sleep(1.0)
o,a=q(b'6'),q(b'7')
print('pads: '+'  '.join(f"{n}={'toggle' if (o>>i)&1 and not (a>>i)&1 else ('1' if (a>>i)&1 else '0')}" for i,n in enumerate(names)))
s.write(b'#'); time.sleep(1.0)
print('DQS sticky OR lane0/1: %02x %02x' % (q(b'0'), q(b'1')))
