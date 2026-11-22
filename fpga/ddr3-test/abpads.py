import serial,time,sys
s=serial.Serial(sys.argv[1],9600,timeout=1); time.sleep(0.2); s.reset_input_buffer()
def q(c):
    s.write(c); b=s.read(1); return b[0] if b else None
print('states', [q(b'?')-0x40 for _ in range(4)])
s.write(b'#'); time.sleep(1.5)
o=q(b'9')|q(b':')<<8|q(b';')<<16; a=q(b'<')|q(b'=')<<8|q(b'>')<<16
names=[f'A{i}' for i in range(14)]+['BA0','BA1','BA2']
print('  '.join(f"{n}={'T' if (o>>i)&1 and not (a>>i)&1 else ('1' if (a>>i)&1 else '0')}" for i,n in enumerate(names)))
