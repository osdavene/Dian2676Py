import math
from urllib.parse import urlencode
from typing import Any, List, Dict, Optional
from fastapi import Request
from markupsafe import Markup


class CallableInt(int):
    def __call__(self) -> int:
        return int(self)


class Paginator:
    """Laravel-compatible Paginator for SQLAlchemy queries and Jinja2 templates."""

    def __init__(
        self,
        query: Any,
        request: Request,
        per_page: int = 15,
        page_param: str = "page",
    ):
        self.request = request
        self.per_page = max(1, per_page)
        self.page_param = page_param

        try:
            raw_page = int(request.query_params.get(page_param, 1))
        except (ValueError, TypeError):
            raw_page = 1
        self.current_page = max(1, raw_page)

        self.total: int = CallableInt(query.count())
        self.last_page: int = CallableInt(max(1, math.ceil(self.total / self.per_page)))
        if self.current_page > self.last_page:
            self.current_page = int(self.last_page)

        offset = (self.current_page - 1) * self.per_page
        self.items: List[Any] = query.offset(offset).limit(self.per_page).all()

        if self.total == 0:
            self.first_item = CallableInt(0)
            self.last_item = CallableInt(0)
        else:
            self.first_item = CallableInt(offset + 1)
            self.last_item = CallableInt(offset + len(self.items))

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __bool__(self):
        return len(self.items) > 0

    def isEmpty(self) -> bool:
        return len(self.items) == 0

    def is_empty(self) -> bool:
        return len(self.items) == 0

    def hasPages(self) -> bool:
        return self.last_page > 1

    def has_pages(self) -> bool:
        return self.last_page > 1

    def onFirstPage(self) -> bool:
        return self.current_page <= 1

    def hasMorePages(self) -> bool:
        return self.current_page < self.last_page

    def total_count(self) -> int:
        return self.total

    def firstItem(self) -> int:
        return self.first_item

    def lastItem(self) -> int:
        return self.last_item

    def url(self, page: int) -> str:
        params = dict(self.request.query_params)
        params[self.page_param] = str(page)
        return f"{self.request.url.path}?{urlencode(params)}"

    def previousPageUrl(self) -> Optional[str]:
        if self.current_page > 1:
            return self.url(self.current_page - 1)
        return None

    def nextPageUrl(self) -> Optional[str]:
        if self.current_page < self.last_page:
            return self.url(self.current_page + 1)
        return None

    def _page_window(self) -> List[Any]:
        """Produces a list of page numbers and '...' separators."""
        if self.last_page <= 7:
            return list(range(1, self.last_page + 1))
        pages: List[Any] = []
        if self.current_page <= 4:
            pages.extend(range(1, 6))
            pages.append("...")
            pages.append(self.last_page)
        elif self.current_page >= self.last_page - 3:
            pages.append(1)
            pages.append("...")
            pages.extend(range(self.last_page - 4, self.last_page + 1))
        else:
            pages.append(1)
            pages.append("...")
            pages.extend(range(self.current_page - 1, self.current_page + 2))
            pages.append("...")
            pages.append(self.last_page)
        return pages

    def links(self) -> Markup:
        if not self.hasPages():
            return Markup("")

        prev_mobile = (
            '<span class="relative inline-flex items-center px-4 py-2 text-xs font-bold text-slate-400 bg-slate-100 border border-slate-200 cursor-default leading-5 rounded-xl">&laquo; Anterior</span>'
            if self.onFirstPage()
            else f'<a href="{self.previousPageUrl()}" class="relative inline-flex items-center px-4 py-2 text-xs font-bold text-slate-700 bg-white border border-slate-300 leading-5 rounded-xl hover:bg-slate-50 transition">&laquo; Anterior</a>'
        )
        next_mobile = (
            f'<a href="{self.nextPageUrl()}" class="relative inline-flex items-center px-4 py-2 ml-3 text-xs font-bold text-slate-700 bg-white border border-slate-300 leading-5 rounded-xl hover:bg-slate-50 transition">Siguiente &raquo;</a>'
            if self.hasMorePages()
            else '<span class="relative inline-flex items-center px-4 py-2 ml-3 text-xs font-bold text-slate-400 bg-slate-100 border border-slate-200 cursor-default leading-5 rounded-xl">Siguiente &raquo;</span>'
        )

        prev_desktop = (
            '<span aria-disabled="true"><span class="relative inline-flex items-center px-3 py-2 text-xs font-bold text-slate-300 bg-slate-50 border border-slate-200 cursor-default rounded-l-xl leading-5" aria-hidden="true"><i class="fa-solid fa-chevron-left"></i></span></span>'
            if self.onFirstPage()
            else f'<a href="{self.previousPageUrl()}" rel="prev" class="relative inline-flex items-center px-3 py-2 text-xs font-bold text-slate-600 bg-white border border-slate-200 rounded-l-xl leading-5 hover:bg-slate-50 hover:text-blue-600 transition"><i class="fa-solid fa-chevron-left"></i></a>'
        )

        page_items = []
        for p in self._page_window():
            if p == "...":
                page_items.append(
                    '<span aria-disabled="true"><span class="relative inline-flex items-center px-3.5 py-2 -ml-px text-xs font-bold text-slate-400 bg-white border border-slate-200 cursor-default leading-5">...</span></span>'
                )
            elif p == self.current_page:
                page_items.append(
                    f'<span aria-current="page"><span class="relative inline-flex items-center px-3.5 py-2 -ml-px text-xs font-black text-white bg-blue-600 border border-blue-600 cursor-default leading-5">{p}</span></span>'
                )
            else:
                page_items.append(
                    f'<a href="{self.url(p)}" class="relative inline-flex items-center px-3.5 py-2 -ml-px text-xs font-bold text-slate-600 bg-white border border-slate-200 leading-5 hover:bg-slate-50 hover:text-blue-600 transition">{p}</a>'
                )

        next_desktop = (
            f'<a href="{self.nextPageUrl()}" rel="next" class="relative inline-flex items-center px-3 py-2 -ml-px text-xs font-bold text-slate-600 bg-white border border-slate-200 rounded-r-xl leading-5 hover:bg-slate-50 hover:text-blue-600 transition"><i class="fa-solid fa-chevron-right"></i></a>'
            if self.hasMorePages()
            else '<span aria-disabled="true"><span class="relative inline-flex items-center px-3 py-2 -ml-px text-xs font-bold text-slate-300 bg-slate-50 border border-slate-200 cursor-default rounded-r-xl leading-5" aria-hidden="true"><i class="fa-solid fa-chevron-right"></i></span></span>'
        )

        html = f"""
        <nav role="navigation" aria-label="Pagination Navigation" class="flex items-center justify-between">
            <div class="flex justify-between flex-1 sm:hidden">
                {prev_mobile}
                {next_mobile}
            </div>
            <div class="hidden sm:flex-1 sm:flex sm:items-center sm:justify-between">
                <div>
                    <p class="text-xs text-slate-500 font-medium">
                        Mostrando <span class="font-bold text-slate-800">{self.first_item}</span> a <span class="font-bold text-slate-800">{self.last_item}</span> de <span class="font-bold text-slate-800">{self.total}</span> resultados
                    </p>
                </div>
                <div>
                    <span class="relative z-0 inline-flex rounded-xl shadow-sm">
                        {prev_desktop}
                        {"".join(page_items)}
                        {next_desktop}
                    </span>
                </div>
            </div>
        </nav>
        """
        return Markup(html)
