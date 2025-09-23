import os, pathlib
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.docstore.document import Document
from app.config import settings

KB_DOCS = [
    ("services.md", """
# Barber Shop Services & Policies

We offer Haircut, Beard Trim, Hot Towel Shave, Kids Haircut, Wash & Style, and Color Touch‑up.
Walk‑ins welcome subject to availability; appointments preferred.
Arrive 5 minutes early.
Cancellations with <2 hours notice may incur a $10 fee.
Payment: cards and cash.
"""),
    ("faq.md", """
# FAQs
Q: Do you take walk‑ins? A: Yes, when available.
Q: Do you do hair styling and cut? A: Ask for "Haircut" or "Wash & Style"; stylist can style during the Haircut service.
Q: Kids pricing? A: Kids Haircut is discounted.
"""),
]

class RAG:
    def __init__(self, index_path: str):
        self.index_path = index_path
        self.emb = OpenAIEmbeddings(
            model="text-embedding-3-large",
            openai_api_key=settings.openai_api_key
        )
        self.store: FAISS | None = None

    def build(self):
        docs = [Document(page_content=txt, metadata={"source": name}) for name, txt in KB_DOCS]
        self.store = FAISS.from_documents(docs, self.emb)
        pathlib.Path(self.index_path).mkdir(parents=True, exist_ok=True)
        self.store.save_local(self.index_path)

    def load(self):
        if os.path.exists(self.index_path):
            self.store = FAISS.load_local(self.index_path, self.emb, allow_dangerous_deserialization=True)
        else:
            self.build()

    def query(self, q: str, k: int = 4) -> str:
        if self.store is None:
            self.load()
        hits = self.store.similarity_search(q, k=k)
        context = "\n\n".join([f"[#{i+1}] from {h.metadata.get('source')}:\n{h.page_content}" for i, h in enumerate(hits)])
        return context