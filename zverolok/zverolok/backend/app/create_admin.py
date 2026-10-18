import asyncio,os
from sqlalchemy import select
from app.main import Session, User, engine, Base, ph
async def main():
    username=os.environ['ADMIN_USERNAME'];password=os.environ['ADMIN_PASSWORD']
    if len(password)<20:raise ValueError('Admin password must be 20+ characters')
    async with Session() as s:
        user=await s.scalar(select(User).where(User.email==username))
        if user:user.password_hash=ph.hash(password);user.role='admin'
        else:s.add(User(email=username,password_hash=ph.hash(password),role='admin'))
        await s.commit()
    print('Admin provisioned')
if __name__=='__main__':asyncio.run(main())
