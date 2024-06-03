from typing import List, Annotated
from sqlalchemy.orm import Session
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
from uuid import UUID, uuid4
from database import SessionLocal, engine
import models
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials = True,
    allow_methods=['*'],
    allow_headers=['*']
)

class PersonalityTrait(BaseModel):
    label: str
    task_id: int
    trait_id: int

class TaskBase(BaseModel):
    sentiment: str
    category: str
    label: str
    context: str
    user_id: UUID
    time_of_day: str
    amount_of_time: str
    day_of_week: str

class TaskModel(TaskBase):
    task_id: int
    
    class Config:
        orm_mode = True

class PersonBase(BaseModel):
    user_name: str
    email: str
    person_tasks: List[TaskModel]

class PersonModel(PersonBase):
    user_id: UUID
    
    class Config:
        orm_mode = True

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally: 
        db.close()

db_dependency = Annotated[Session, Depends(get_db)]

models.Base.metadata.create_all(bind=engine)

@app.post("/tasks/", response_model=TaskModel)
async def create_task(task: TaskBase, db: db_dependency):
    db_transaction = models.Task(**task.dict())
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)
    return db_transaction


@app.post("/users/", response_model=PersonModel)
async def create_person(person: PersonBase, db: db_dependency):
    db_transaction = models.Person(**person.dict())
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)
    return db_transaction

@app.get("/users", response_model=List[PersonModel])
async def read_users(db: db_dependency, skip: int = 0, limit: int = 100):
    users = db.query(models.Person).offset(skip).limit(limit).all()
    return users


@app.get("/tasks", response_model=List[TaskModel])
async def read_tasks(db: db_dependency, skip: int = 0, limit: int = 100):
    tasks = db.query(models.Task).offset(skip).limit(limit).all()
    return tasks


@app.get("/user_by_id/{user_id}", response_model=PersonModel)
async def user_by_id(user_id,db: db_dependency):
    user = db.query(models.Person).filter(models.Person.user_id == UUID(user_id)).first()
    return user

@app.get("/user_by_username/{username}", response_model=PersonModel)
async def user_by_user_name(username,db: db_dependency):
    user = db.query(models.Person).filter(models.Person.user_name == username).first()
    return user