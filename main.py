from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from itinerary import generate_itinerary
from models import SessionLocal, Group
from typing import Optional
from models import SessionLocal, Group, User, GroupMember, Expense, ExpenseSplit
from datetime import date, timedelta
from pydantic import BaseModel, model_validator

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


MAX_TRIP_DAYS = 14


class TripRequest(BaseModel):
    name: str
    destination: str
    start_date: str
    end_date: str
    budget: str
    interests: str
    trip_personality: str

    @model_validator(mode="after")
    def check_dates(self):
        try:
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
        except ValueError:
            raise ValueError("Dates must be in YYYY-MM-DD format")

        # One day of grace: the server runs in UTC, so a user's "today" can be
        # "yesterday" on the server in the early morning
        if start < date.today() - timedelta(days=1):
            raise ValueError("Start date cannot be in the past")
        if end < start:
            raise ValueError("End date cannot be before the start date")
        if (end - start).days + 1 > MAX_TRIP_DAYS:
            raise ValueError(f"Trips can be at most {MAX_TRIP_DAYS} days long")
        return self


@app.get("/")
def read_root():
    return {"message": "Trip planner API is running"}


@app.post("/trips")
def create_trip(trip: TripRequest):
    # Step 1: generate the itinerary with the AI
    try:
        itinerary = generate_itinerary(
            destination=trip.destination,
            start_date=trip.start_date,
            end_date=trip.end_date,
            budget=trip.budget,
            interests=trip.interests,
            trip_personality=trip.trip_personality,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Itinerary generation failed: {e}")

    # Step 2: save the trip (a Group row) with its itinerary
    db = SessionLocal()
    new_trip = Group(
        name=trip.name,
        destination=trip.destination,
        start_date=trip.start_date,
        end_date=trip.end_date,
        budget=trip.budget,
        trip_personality=trip.trip_personality,
        itinerary=itinerary,
    )
    db.add(new_trip)
    db.commit()
    db.refresh(new_trip)

    # Grab the id while the session is still open
    trip_id = new_trip.id
    db.close()

    return {"trip_id": trip_id, "itinerary": itinerary}


@app.get("/trips/{trip_id}")
def get_trip(trip_id: int):
    db = SessionLocal()
    trip = db.query(Group).filter(Group.id == trip_id).first()

    if trip is None:
        db.close()
        raise HTTPException(status_code=404, detail="Trip not found")

    result = {
        "trip_id": trip.id,
        "name": trip.name,
        "destination": trip.destination,
        "start_date": trip.start_date,
        "end_date": trip.end_date,
        "budget": trip.budget,
        "trip_personality": trip.trip_personality,
        "itinerary": trip.itinerary,
    }
    db.close()
    return result

class MemberRequest(BaseModel):
    name: str
    email: Optional[str] = None


class ExpenseRequest(BaseModel):
    paid_by_user_id: int
    description: str
    amount: float
    split_between: Optional[list[int]] = None  # None = split among all members


def get_member_ids(db, trip_id: int) -> list[int]:
    rows = db.query(GroupMember).filter(GroupMember.group_id == trip_id).all()
    return [row.user_id for row in rows]


@app.post("/trips/{trip_id}/members")
def add_member(trip_id: int, member: MemberRequest):
    db = SessionLocal()
    try:
        trip = db.query(Group).filter(Group.id == trip_id).first()
        if trip is None:
            raise HTTPException(status_code=404, detail="Trip not found")

        user = None
        if member.email:
            user = db.query(User).filter(User.email == member.email).first()
        if user is None:
            user = User(name=member.name, email=member.email)
            db.add(user)
            db.commit()
            db.refresh(user)

        if user.id in get_member_ids(db, trip_id):
            raise HTTPException(status_code=400, detail="User is already in this trip")

        db.add(GroupMember(group_id=trip_id, user_id=user.id))
        db.commit()
        return {"user_id": user.id, "name": user.name, "trip_id": trip_id}
    finally:
        db.close()


@app.post("/trips/{trip_id}/expenses")
def add_expense(trip_id: int, expense: ExpenseRequest):
    if expense.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    db = SessionLocal()
    try:
        trip = db.query(Group).filter(Group.id == trip_id).first()
        if trip is None:
            raise HTTPException(status_code=404, detail="Trip not found")

        member_ids = get_member_ids(db, trip_id)
        split_ids = list(dict.fromkeys(expense.split_between or member_ids))

        if expense.paid_by_user_id not in member_ids:
            raise HTTPException(status_code=400, detail="Payer is not a member of this trip")
        if not split_ids or any(uid not in member_ids for uid in split_ids):
            raise HTTPException(status_code=400, detail="Everyone in the split must be a trip member")

        new_expense = Expense(
            group_id=trip_id,
            paid_by_user_id=expense.paid_by_user_id,
            description=expense.description,
            amount=expense.amount,
        )
        db.add(new_expense)
        db.commit()
        db.refresh(new_expense)

        # Equal split, calculated in cents so the shares always add up exactly
        total_cents = round(expense.amount * 100)
        base_share = total_cents // len(split_ids)
        remainder = total_cents - base_share * len(split_ids)

        for i, uid in enumerate(split_ids):
            share_cents = base_share + (1 if i < remainder else 0)
            db.add(ExpenseSplit(
                expense_id=new_expense.id,
                user_id=uid,
                amount_owed=share_cents / 100,
            ))
        db.commit()

        return {"expense_id": new_expense.id, "split_between": split_ids}
    finally:
        db.close()


@app.get("/trips/{trip_id}/balances")
def get_balances(trip_id: int):
    db = SessionLocal()
    try:
        trip = db.query(Group).filter(Group.id == trip_id).first()
        if trip is None:
            raise HTTPException(status_code=404, detail="Trip not found")

        expenses = db.query(Expense).filter(Expense.group_id == trip_id).all()
        expense_ids = [e.id for e in expenses]
        splits = (
            db.query(ExpenseSplit).filter(ExpenseSplit.expense_id.in_(expense_ids)).all()
            if expense_ids else []
        )

        # net > 0: is owed money, net < 0: owes money
        net = {uid: 0.0 for uid in get_member_ids(db, trip_id)}
        for e in expenses:
            net[e.paid_by_user_id] += e.amount
        for s in splits:
            net[s.user_id] -= s.amount_owed

        names = {
            u.id: u.name
            for u in db.query(User).filter(User.id.in_(list(net.keys()))).all()
        }
    finally:
        db.close()

    # Work in cents to avoid floating point drift
    cents = {uid: round(v * 100) for uid, v in net.items()}
    creditors = sorted([[uid, c] for uid, c in cents.items() if c > 0], key=lambda x: -x[1])
    debtors = sorted([[uid, -c] for uid, c in cents.items() if c < 0], key=lambda x: -x[1])

    payments = []
    i = j = 0
    while i < len(creditors) and j < len(debtors):
        pay = min(creditors[i][1], debtors[j][1])
        payments.append({
            "from": names[debtors[j][0]],
            "to": names[creditors[i][0]],
            "amount": pay / 100,
        })
        creditors[i][1] -= pay
        debtors[j][1] -= pay
        if creditors[i][1] == 0:
            i += 1
        if debtors[j][1] == 0:
            j += 1

    return {
        "balances": {names[uid]: c / 100 for uid, c in cents.items()},
        "payments": payments,
    }
    