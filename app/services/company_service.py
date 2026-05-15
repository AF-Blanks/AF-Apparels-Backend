"""CompanyService — admin management of wholesale company accounts."""
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.company import Company, CompanyUser
from app.models.order import Order
from app.models.user import User
from app.schemas.company import CompanyUpdate, SuspendRequest


class CompanyService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_companies_paginated(
        self,
        q: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list, int]:
        # Build base filter
        filters = []
        if q:
            filters.append(Company.name.ilike(f"%{q}%"))
        if status:
            filters.append(Company.status == status)

        # Subqueries for order stats
        order_count_sub = (
            select(Order.company_id, func.count(Order.id).label("order_count"))
            .group_by(Order.company_id)
            .subquery()
        )
        total_spend_sub = (
            select(
                Order.company_id,
                func.coalesce(func.sum(Order.total), 0).label("total_spend"),
            )
            .where(Order.payment_status == "paid")
            .group_by(Order.company_id)
            .subquery()
        )

        query = (
            select(
                Company,
                func.coalesce(order_count_sub.c.order_count, 0).label("order_count"),
                func.coalesce(total_spend_sub.c.total_spend, 0).label("total_spend"),
            )
            .outerjoin(order_count_sub, Company.id == order_count_sub.c.company_id)
            .outerjoin(total_spend_sub, Company.id == total_spend_sub.c.company_id)
        )
        if filters:
            query = query.where(*filters)

        count_q = select(func.count(Company.id))
        if filters:
            count_q = count_q.where(*filters)
        total_result = await self.db.execute(count_q)
        total = total_result.scalar_one()

        query = query.offset((page - 1) * page_size).limit(page_size).order_by(Company.name)
        result = await self.db.execute(query)
        rows = result.all()

        # Fetch owner user info for each company in one batch query
        company_ids = [row[0].id for row in rows]
        owner_map: dict = {}
        if company_ids:
            owner_result = await self.db.execute(
                select(CompanyUser.company_id, User.email, User.first_name, User.last_name, User.phone, User.account_type)
                .join(User, CompanyUser.user_id == User.id)
                .where(CompanyUser.company_id.in_(company_ids), CompanyUser.role == "owner", CompanyUser.is_active == True)
            )
            for comp_id, email, first, last, phone, acct_type in owner_result.all():
                owner_map[comp_id] = {
                    "email": email,
                    "phone": phone,
                    "contact_name": f"{first} {last}".strip() or None,
                    "account_type": acct_type or "wholesale",
                }

        # Fetch last order date per company
        last_order_result = await self.db.execute(
            select(Order.company_id, func.max(Order.created_at).label("last_order_date"))
            .where(Order.company_id.in_(company_ids))
            .group_by(Order.company_id)
        )
        last_order_map = {row[0]: row[1] for row in last_order_result.all()}

        # Build list of dicts that match CompanyListItem schema
        companies = []
        for row in rows:
            company = row[0]
            owner = owner_map.get(company.id, {})
            companies.append({
                "id": company.id,
                "name": company.name,
                "status": company.status,
                "pricing_tier_id": company.pricing_tier_id,
                "shipping_tier_id": company.shipping_tier_id,
                "order_count": int(row[1]),
                "total_spend": Decimal(str(row[2])),
                "created_at": company.created_at,
                "email": owner.get("email"),
                "phone": owner.get("phone"),
                "contact_name": owner.get("contact_name"),
                "last_order_date": last_order_map.get(company.id),
                "tags": company.tags or [],
                "account_type": owner.get("account_type", "wholesale"),
            })

        # Include retail users who have no company record — only when no company-specific filters applied
        if not q and not status:
            # Exclude retail users who already have a Company (created during activation)
            retail_with_company = select(CompanyUser.user_id).where(CompanyUser.role == "owner").subquery()
            retail_result = await self.db.execute(
                select(User).where(
                    User.account_type == "retail",
                    User.is_active == True,
                    User.id.not_in(retail_with_company),
                ).order_by(User.created_at.desc())
            )
            retail_users = retail_result.scalars().all()

            # Fetch order counts for retail users
            retail_ids = [u.id for u in retail_users]
            retail_order_counts: dict = {}
            retail_order_totals: dict = {}
            if retail_ids:
                rc = await self.db.execute(
                    select(Order.placed_by_id, func.count(Order.id).label("cnt"))
                    .where(Order.placed_by_id.in_(retail_ids))
                    .group_by(Order.placed_by_id)
                )
                for uid, cnt in rc.all():
                    retail_order_counts[uid] = cnt
                rs = await self.db.execute(
                    select(Order.placed_by_id, func.coalesce(func.sum(Order.total), 0).label("total"))
                    .where(Order.placed_by_id.in_(retail_ids), Order.payment_status == "paid")
                    .group_by(Order.placed_by_id)
                )
                for uid, tot in rs.all():
                    retail_order_totals[uid] = tot

            for u in retail_users:
                companies.append({
                    "id": u.id,
                    "name": f"{u.first_name} {u.last_name}".strip() or u.email,
                    "status": "active",
                    "pricing_tier_id": None,
                    "shipping_tier_id": None,
                    "order_count": retail_order_counts.get(u.id, 0),
                    "total_spend": Decimal(str(retail_order_totals.get(u.id, 0))),
                    "created_at": u.created_at,
                    "email": u.email,
                    "phone": u.phone,
                    "contact_name": None,
                    "last_order_date": None,
                    "tags": [],
                    "account_type": "retail",
                })
            total += len(retail_users)

        return companies, total

    async def _get_company_orm(self, company_id: UUID) -> Company:
        result = await self.db.execute(
            select(Company).where(Company.id == company_id)
        )
        company = result.scalar_one_or_none()
        if not company:
            raise NotFoundError(f"Company {company_id} not found")
        return company

    async def get_company_detail(self, company_id: UUID) -> dict:
        from app.models.pricing import PricingTier
        company = await self._get_company_orm(company_id)

        discount_percent = None
        if company.pricing_tier_id:
            tier_result = await self.db.execute(
                select(PricingTier).where(PricingTier.id == company.pricing_tier_id)
            )
            tier = tier_result.scalar_one_or_none()
            if tier:
                discount_percent = float(getattr(tier, "discount_percent", 0) or 0)

        from app.schemas.company import CompanyDetail
        data = CompanyDetail.model_validate(company).model_dump()
        data["discount_percent"] = discount_percent
        return data

    async def update_company_tiers(self, company_id: UUID, data: CompanyUpdate) -> Company:
        company = await self._get_company_orm(company_id)
        update_fields = data.model_dump(exclude_unset=True)
        for field, value in update_fields.items():
            setattr(company, field, value)
        await self.db.flush()
        await self.db.refresh(company)
        if "tags" in update_fields:
            from app.core.redis import redis_delete
            await redis_delete(f"company:{company_id}:discount_group_id")
        return company

    async def suspend(self, company_id: UUID, reason: str) -> Company:
        company = await self._get_company_orm(company_id)
        company.status = "suspended"
        await self.db.flush()
        return company

    async def reactivate(self, company_id: UUID) -> Company:
        company = await self._get_company_orm(company_id)
        company.status = "active"
        await self.db.flush()
        return company

    async def get_order_stats(self, company_id: UUID) -> dict:
        count_result = await self.db.execute(
            select(func.count(Order.id)).where(Order.company_id == company_id)
        )
        total_result = await self.db.execute(
            select(func.coalesce(func.sum(Order.total), 0)).where(
                Order.company_id == company_id, Order.payment_status == "paid"
            )
        )
        return {
            "order_count": count_result.scalar_one(),
            "total_spend": total_result.scalar_one(),
        }
