from database import Base
from sqlalchemy import Column, String, Integer,ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, mapped_column
import uuid

class PersonalityTrait(Base):
    __tablename__ = 'personality_traits'
    task_id=mapped_column(Integer,  ForeignKey("tasks.task_id"))
    trait_id= Column(Integer, primary_key=True, index=True)
    label= Column(String)

class Task(Base):
    __tablename__ = 'tasks'

    task_id= mapped_column(Integer, primary_key=True, index=True)
    user_id= mapped_column(UUID, ForeignKey("person.user_id"))
    category=Column(String)
    label=Column(String)
    time_of_day=Column(String)
    amount_of_time=Column(String)
    day_of_week=Column(String)
    context= Column(String)
    sentiment=Column(String)
    personality_traits = relationship(
        "PersonalityTrait",
        primaryjoin="and_(PersonalityTrait.task_id==Task.task_id)",
    )

class Person(Base):
    __tablename__ = 'person'

    user_name= Column(String)
    user_id= mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email=Column(String)
    person_tasks = relationship(
        "Task",
        primaryjoin="and_(Task.user_id==Person.user_id)",
    )