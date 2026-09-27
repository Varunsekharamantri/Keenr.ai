import yaml
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel
from ..config import settings

class InitiativeDef(BaseModel):
    id: str
    name: str
    it_offering: str
    keywords: List[str]

class CategoryDef(BaseModel):
    id: str
    name: str
    description: str
    initiatives: List[InitiativeDef]

class TaxonomyManager:
    def __init__(self, taxonomy_file: Path = settings.TAXONOMY_PATH):
        self.taxonomy_file = taxonomy_file
        self.categories: List[CategoryDef] = []
        self.initiatives_by_id: Dict[str, InitiativeDef] = {}
        self.category_by_initiative_id: Dict[str, CategoryDef] = {}
        self.keyword_to_initiatives: Dict[str, List[InitiativeDef]] = {}
        self.load_taxonomy()

    def load_taxonomy(self):
        if not self.taxonomy_file.exists():
            return
        
        with open(self.taxonomy_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            
        self.categories = []
        self.initiatives_by_id = {}
        self.category_by_initiative_id = {}
        self.keyword_to_initiatives = {}
        
        for cat_data in data.get("categories", []):
            initiatives = []
            for init_data in cat_data.get("initiatives", []):
                init_obj = InitiativeDef(
                    id=init_data["id"],
                    name=init_data["name"],
                    it_offering=init_data.get("it_offering", ""),
                    keywords=[k.lower().strip() for k in init_data.get("keywords", [])]
                )
                initiatives.append(init_obj)
                self.initiatives_by_id[init_obj.id] = init_obj
                
                # Index keywords
                for kw in init_obj.keywords:
                    if kw not in self.keyword_to_initiatives:
                        self.keyword_to_initiatives[kw] = []
                    self.keyword_to_initiatives[kw].append(init_obj)
                    
            cat_obj = CategoryDef(
                id=cat_data["id"],
                name=cat_data["name"],
                description=cat_data.get("description", ""),
                initiatives=initiatives
            )
            self.categories.append(cat_obj)
            
            for init_obj in initiatives:
                self.category_by_initiative_id[init_obj.id] = cat_obj

    def get_initiative(self, initiative_id: str) -> Optional[InitiativeDef]:
        return self.initiatives_by_id.get(initiative_id)

    def get_category_for_initiative(self, initiative_id: str) -> Optional[CategoryDef]:
        return self.category_by_initiative_id.get(initiative_id)

    def get_all_categories(self) -> List[CategoryDef]:
        return self.categories

# Global singleton instance
taxonomy_manager = TaxonomyManager()
