#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import ast
import db
import json
import MySQLdb
import sys
import time
import urllib
import yaml

db.parser.add_argument("--related", help="Only compute related table", action="store_true")
db.parser.add_argument("--populate_has_data", help="Updates values so we know for which data we have somthing to show", action="store_true")
db.connect(False)
print db.db

log_file = open("log.txt", "a")
def log(s, only_file=False):
    if not only_file:
        try:
            print "LOG: " + s
        except:
            print "LOG: " + s.encode("utf8")
    log_file.write(s.encode("utf8") + "\n")

api_calls = 0

class IdMaster:

    name_address = {}
    name_lat_lng = {}
    address_data = {}

    def __init__(self):
        #load table to memory
        def loadFromTo(from_offset, batch_size):
            log("Loading ids from " + str(from_offset))
            cur = db.getCursor()
            db.execute(cur, "SELECT id, entity_name, address, original_address, lat, lng " + #, json
                        "FROM entities " +
                        "LIMIT " + str(batch_size) + " OFFSET " + str(from_offset))
            processed = False
            for row in cur.fetchall():
                processed = True
                norm_name = self.normalize(row["entity_name"])
                norm_address = self.normalize(row["address"])
                norm_orig_address = self.normalize(row["original_address"])
                t_address = (hash(norm_name), hash(norm_address))
                t_orig_address = (hash(norm_name), hash(norm_orig_address))
                t_lat_lng = (hash(norm_name), hash((str(row["lat"]), str(row["lng"]))))
                self.name_address[t_address] = row["id"]
                self.name_address[t_orig_address] = row["id"]
                self.name_lat_lng[t_lat_lng] = row["id"]
                self.address_data[hash(norm_orig_address)] = row["id"]
            return processed    
        
        from_index = 0
        batch_size = 66666
        while loadFromTo(from_index, batch_size):
            from_index += batch_size
        log("Loading done")


    def normalize(self, s):
        return s.lower().replace(" ", "").replace("."," ").replace(",", "")

    def getJSONForId(self, row_id):
        log("getJSONForId: " + str(row_id))
        cur = db.getCursor()
        cur = db.execute(cur, "SELECT json FROM entities WHERE id=%s", [row_id])
        return json.loads(cur.fetchone()["json"])

    def geocode(self, address, mock=True):
        global api_calls
        log("Geocoding: " + address)
        if mock:
            # TODO: fix unicode
            with open('tmp/address.json') as data_file:
                    text = data_file.read()
                    d = ast.literal_eval(text)
                    return d["results"][0]
            return ""
       
        split = address.split(" ")
        for i in range(len(split)):
            attempted = " ".join(split[i:])    
            norm_address = self.normalize(attempted)
            if (hash(norm_address) in self.address_data):
                return self.getJSONForId(self.address_data[hash(norm_address)])
            # TODO: also add viewport biasing to slovakia
            params = {
                'address': attempted.encode('utf-8'),
                'region': 'sk',
                'key': 'TODO: read key'
            }
            url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.urlencode(params)
            try:
                response = urllib.urlopen(url)
                data = json.loads(response.read())
                api_calls += 1
                if data["status"] == 'OK':
                    return data["results"]
            except:
                pass
            log("Unable to geocode: (" + attempted + ") removing first word")

        return None

    def getId(self, name, address):
        def toUnicode(s):
          if isinstance(s, str): return s.decode("utf8")
          return s

        name = toUnicode(name)
        address = toUnicode(address)

        norm_name = self.normalize(name)
        norm_address = self.normalize(address)
        t_address = (hash(norm_name), hash(norm_address))
        if t_address in self.name_address:
            return self.name_address[t_address]
        djson = self.geocode(address, False)
        if djson is None:
            log("Address " + address + "geocoded to None")
            return None
        g = djson[0]
        if g is None:
            log("Address " + address + "geocoded to None")
            return None
        lat_n = g["geometry"]["location"]["lat"]
        lng_n = g["geometry"]["location"]["lng"]
        lat = "%3.7f" % lat_n
        lng = "%3.7f" % lng_n

        log(address + " -> " + str(lat) + ", " + str(lng))

        t_lat_lng = (hash(norm_name), hash((lat, lng)))
        if (t_lat_lng) in self.name_lat_lng:
            return self.name_lat_lng[t_lat_lng]
        #add to table
        data = {"address" : g["formatted_address"],
                "original_address": address,
                "entity_name": name,
                "json": json.dumps(djson),
                "lat": lat,
                "lng": lng} 
        row_id = int(db.insertDictionary("entities", data))
        log("Added id " + str(row_id))
        self.address_data[hash(norm_address)] = row_id
        self.name_address[t_address] = row_id
        self.name_lat_lng[t_lat_lng] = row_id
        cursor = db.getCursor()
        db.execute(cursor, "UPDATE entities SET eid=%s WHERE id=%s", [row_id, row_id])
        db.db.commit()
        return row_id

def getGeocodedIds(table_name):
    log("getGeocodedIds " + table_name)
    result = set()
    cur = db.getCursor()
    from_index = 0
    batch_size = 33333
    while True:
      sql = "SELECT orig_id FROM " + table_name + \
             " LIMIT " + str(batch_size) + " OFFSET " + str(from_index)
      db.execute(cur, sql)
      processed = False;
      for row in cur.fetchall():
          processed = True
          result.add(row["orig_id"])
      if not processed: break
      from_index += batch_size
    return result

master = None

# TODO: document all the params
def geocodeTable(
    input_table, name_column, address_column, id_column, source_name,
    new_table_name, extra_columns, max_process=None, address_like=None,
    address_like_column=None, geocoded_table=None):
    global api_calls, master

    cur = db.getCursor()
    if id_column is not None:
        geocoded_id_table = input_table + "_geocoded_" if geocoded_table is None else geocoded_table
        if not db.checkTableExists(db.db, geocoded_id_table):
            log("Creating table: " + geocoded_id_table)
            db.execute(cur, 
                    "CREATE TABLE " + geocoded_id_table + "("
                    "orig_id " + db.getColumnType(input_table, id_column) + " PRIMARY KEY,"
                    "new_id INT)")
            db.db.commit()

    
    if ((new_table_name is not None) and
        (not db.checkTableExists(db.db, new_table_name))):
        new_table_sql = "CREATE TABLE " + new_table_name + " ( id INT"
        for column in extra_columns:
            new_table_sql += (
                "," + extra_columns[column] + " " + db.getColumnType(input_table, column))
        new_table_sql += ", FOREIGN KEY (id) REFERENCES entities(id)) DEFAULT CHARSET=utf8"
        log("Creatting table: " + new_table_name)
        log(new_table_sql)
        db.execute(cur, new_table_sql)
        db.db.commit()

    if (db.getColumnType("entities", source_name) is None):
        db.execute(cur, "ALTER TABLE entities ADD " + source_name + " BOOL")
        db.db.commit()

    select_sql = \
            "SELECT " + \
            (",".join([id_column, name_column + " as name", address_column + " as address"] + \
                      extra_columns.keys())) + \
            " FROM " + input_table

    if address_like is not None:
        select_sql += " WHERE " + \
                (address_column if address_like_column is None else address_like_column) + \
                " LIKE \"%" + address_like + "%\""
    else:
        # Requires address is not null
        select_sql += " WHERE " + address_column + " IS NOT NULL"
    log(select_sql)
    processed_now = [0]
    not_geocoded = 0
    geocoded_ids = getGeocodedIds(geocoded_id_table)
    
    def processFromTo(select_sql, offset, limit, remaining):
      global api_calls
      limit_sql = " LIMIT " + str(limit) + " OFFSET " + str(offset)
      log(limit_sql)
      select_sql += limit_sql
      db.execute(cur, select_sql)
      processed = 0
      not_geocoded = 0
      skipped = 0
      for row in cur.fetchall():
          api_calls_before = api_calls
          processed += 1
          if (row[id_column] in geocoded_ids):
              skipped += 1
              continue
          to_id = master.getId(row["name"], row["address"])
          if (to_id is None): not_geocoded += 1
          if id_column is not None:
              db.insertDictionary(geocoded_id_table,
                  {"orig_id": row[id_column], "new_id": to_id})
          if (new_table_name is not None):
              new_data = {"id": to_id}
              for column in extra_columns:
                  new_data[extra_columns[column]] = row[column]
              db.insertDictionary(new_table_name, new_data)
          
          db.execute(cur, 
              "UPDATE entities SET " + source_name + "=1 WHERE id=%s",
              [to_id])
          geocoded_ids.add(row[id_column])
          db.db.commit()
  
          remaining -= (api_calls - api_calls_before)
          # processed requested number of api calls. stop
          if (remaining <= 0): return False, 0

      log("Total number of rows processed: " + str(processed) +
          ", remaining: " + str(remaining) + ", skipped: " + str(skipped))
      log("Not geocoded: " + str(not_geocoded))
      return processed > 0, remaining

    from_row = 0
    process_in_step = 10000
    remaining = max_process
    while remaining > 0:
      more_data, remaining = processFromTo(
          select_sql, from_row, process_in_step, remaining)
      from_row += process_in_step
      log("More data: " + str(more_data) + " remaining " + str(remaining))
      if (not more_data): break


def nullOrEmpty(x):
    return "if (" + x + " is NULL, \"\", " + x + ")" 

def getConcat(x, y, z=None):
    s="concat(" + nullOrEmpty(x) + ", \" \", " + nullOrEmpty(y)
    if not z is None: s += ", \" \", " + nullOrEmpty(z)
    s += ")"
    return s 

def getConcatList(l):
  s = ", \" \", ".join([nullOrEmpty(x) for x in l])
  return "concat(" + s + ")"

def getNewId(table_name):
    return table_name + ".new_id"

def getMapping(table_name):
    result = {}
    cur = db.getCursor()
    from_index = 0
    batch_size = 33333
    while True:
      sql = "SELECT new_id, orig_id FROM " + table_name + \
             " LIMIT " + str(batch_size) + " OFFSET " + str(from_index)
      db.execute(cur, sql)
      processed = False;
      for row in cur.fetchall():
          processed = True
          result[row["orig_id"]] = row["new_id"]
      if not processed: break
      from_index += batch_size
    return result

def populateRelated(relationship_table, colA, colB, tableA, tableB):
    print "populateRelated", relationship_table
    cur = db.getCursor()
    mapSql = "SELECT id, eid FROM entities"
    db.execute(cur, mapSql)
    id_to_eid = {}
    for row in cur.fetchall():
       id_to_eid[row["id"]] = row["eid"]

    print "loading mapping"
    mapA = getMapping(tableA)
    mapB = getMapping(tableB)
    print "mapping loaded"
    sql = "SELECT " + colA + ", " + colB + " FROM " + relationship_table
    cur = db.getCursor()
    db.execute(cur, sql)
    index = 0
    for row in cur.fetchall(): 
        index += 1
        if (index % 50 == 0): 
          print "index", index
          db.db.commit()
        valA = row.get(colA, None)
        valB = row.get(colB, None)
        if (valA is not None) and (valB is not None) and (valA in mapA) and (valB in mapB):
            newA = mapA[valA]
            newB = mapB[valB]
            if not newA in id_to_eid:
              log("Missing " + str(newA) + " in id_to_eid")
              continue
            if not newB in id_to_eid:
              log("Missing " + str(newB) + " in id_to_eid")
              continue
            db.insertDictionary("related",
                {"id1": newA, "eid1": id_to_eid[newA],
                 "id2": newB, "eid2": id_to_eid[newB]})
    db.db.commit()
    
def processRelated():
    log("processRelated")
    cur = db.getCursor()
    db.execute(cur, "DELETE FROM related")
    db.db.commit()
    populateRelated("people_esd", "organization_id", "record_id",
        "orsresd_geocoded_", "people_esd_geocoded_")
    populateRelated("orsr_relationships", "id_osoby", "id_firmy",
        "orsr_persons_geocoded_", "firmy_unified2_geocoded_")
    populateRelated("relation", "_record_id", "_record_id",
        "relation_from_geocoded_", "relation_to_geocoded_")

def process(limit):
    afp_original_db_id = "_record_id"

    esd_address = (
        "if(address_formatted is not null, address_formatted, " + \
        getConcatList(["address_street", "address_building_number", "address_municipality",
                       "address_postal_code", "address_country"]) + ")"
    )

    geocodeTable("people_esd",
        "IF(full_name IS NOT NULL, full_name, person_formatted_name)",
        esd_address, "record_id", "people_esd", "people_esd_data",
        {"note": "note"}, limit, address_like=None)


    esd_address = (
        "if(formatted_address is not null, formatted_address, " + \
        getConcatList(["street", "building_number", "municipality", "postal_code", "country"]) + ")"
    )

    # orsr from ekosystem.slovensko.digital
    geocodeTable("orsresd",
        "name", esd_address, "id", "orsresd", "orsresd_data", {"ipo": "ico"}, limit, address_like=None)

   # Old ORSR
  # before geocoding this, check whether there's an overlap with the other ORSR
#    geocodeTable("orsr_persons",
#        "meno", "adresa", "id_osoby", "orsr_persons", None, {}, limit, address_like=None)
#
#    geocodeTable("firmy_unified2",
#        "nazov", "adresa", "id", "orsr_companies", "firmy_data",
#        {"ico": "ico" , "pravna_forma": "pravna_forma", "start": "start", "end": "end"},
#        limit, address_like=None)
    #Zivnostentsky register
    geocodeTable("zrsr",
        "name", getConcat("address1", "address2"), "id",
        "zrsr", "zrsr_data", {"ico": "ico", "active": "active"}, limit, address_like=None)
    return
    #Uzivatelia vyhody - ludia
    geocodeTable("uzivatelia_vyhody",
        "meno", "adresa", "record_id",
        "uzivatelia_vyhody_clovek", "uzivatelia_vyhody_ludia_data",
        {"funkcionar": "funkcionar"}, limit,
        geocoded_table="vyhodia_ludia_geocode", address_like=None)

    #Uzivatelia vyhody - firmy
    geocodeTable("uzivatelia_vyhody",
        "spolocnost", "adresa_spolocnosti", "record_id",
        "uzivatelia_vyhody_firma", "uzivatelia_vyhody_firmy_data",
        {"ico": "ico", "forma": "forma"}, limit,
        geocoded_table="vyhody_firmy_geocode", address_like=None)
    
    return
    # DataNest tables 
    geocodeTable("ds_sponzori_stran",
        getConcat("meno_darcu", "priezvisko_darcu", "firma_darcu"), "adresa_darcu",
        afp_original_db_id, "ds_sponzori_stran", "sponzori_stran_data",
        {"hodnota_daru": "hodnota_daru", "strana": "strana", "rok": "rok"},
        limit, address_like=None)

    geocodeTable("ds_stranicke_clenske_prispevky",
        getConcat("meno", "priezvisko"), getConcat("adresa", "mesto"),
        afp_original_db_id, "ds_stranicke_prispevky", "stranicke_prispevky_data",
        {"strana": "strana", "rok": "rok", "vyska_prispevku": "vyska_prispevku", "mena": "mena"},
        limit, address_like=None)
    
    geocodeTable("ds_advokati",
        getConcat("meno_advokata", "priezvisko_advokata"), getConcat("adresa", "mesto", "psc"),
        "afp_original_db_id", "ds_advokati", "advokati_data",
        {"telefonne_cislo" : "telefonne_cislo"}, limit, address_like=None)

    geocodeTable("ds_nadacie",
        getConcat("meno_spravcu", "priezvisko_spravcu"), "adresa_spravcu",
        afp_original_db_id, "ds_nadacie_spravca", None, {}, limit,
        address_like=None, geocoded_table="ds_nadacie_spravca_geocoded_") 

    geocodeTable("ds_nadacie",
        "nazov_nadacie", "adresa_nadacie", afp_original_db_id, "ds_nadacie", 
        "nadacie_data", {"ico_nadacie": "ico_nadacie", "hodnota_imania": "hodnota_imania",
        "poznamka": "poznamka", "ucel_nadacie": "ucel_nadacie"},
        limit, address_like=None)

    geocodeTable("ds_dotacie_audiovizfond",
        getConcat("first_name", "last_name", "company"),
        getConcat("address", "zip_code", "town"),
        "_record_id", "ds_dotacie_audiovizfond", "audiovizfond_data",
        {"amount": "amount", "currency": "currency", "subsidy_subject": "subsidy_subject", "year": "year"},
        limit, address_like=None)

    geocodeTable("ds_auditori", 
        getConcat("meno", "priezvisko", "firma"), getConcat("adresa", "mesto", "psc"),
        afp_original_db_id, "ds_auditori", "auditori_data",
        {"cislo_licencie" : "cislo_licencie", "typ_auditora" : "typ_auditora"},
        limit, address_like=None)

    return

    geocodeTable("ds_danovi_dlznici", 
        getConcat("meno", "priezvisko"), getConcat("adresa", "mesto"),
        afp_original_db_id, "ds_danovi_dlznici", "danovi_dlznici_data",
        {"danovy_nedoplatok": "danovy_nedoplatok", "zdroj": "zdroj", "mena": "mena"},
        limit, address_like=None) 

# ORSR data
    geocodeTable("relation",
        "rel_name", "rel_address", afp_original_db_id, "new_orsr", None, {},
        limit, address_like=None, address_like_column="city",
        geocoded_table="relation_to_geocoded_")
    
    geocodeTable("relation",
        "name", getConcat("street", "city", "psc"), afp_original_db_id,
        "new_orsr", "new_orsr_data", {"ico": "ico", "url": "url", "start": "start"},
        limit, address_like=None, address_like_column="rel_address",
        geocoded_table="relation_from_geocoded_")


def populateHasData():
    data_sources = yaml.load(open("datasources.yaml", "r"))
    cur = db.getCursor()
    if (db.getColumnType("entities", "has_data") is None):
        db.execute(cur, "ALTER TABLE entities ADD has_data BOOL")
        db.db.commit()

    cur = db.execute(cur, "UPDATE entities SET has_data = NULL")
    db.db.commit()

    print "Populating has_data based on related"
    sql = "UPDATE entities JOIN related ON entities.id = related.id1 SET entities.has_data=true"
    cur = db.execute(cur, sql)
    db.db.commit()
    sql = "UPDATE entities JOIN related ON entities.id = related.id2 SET entities.has_data=true"
    cur = db.execute(cur, sql)
    db.db.commit()

    print "Populating has_data based on contracts"
    sql = "UPDATE entities JOIN contracts ON entities.id = contracts.id SET entities.has_data=true" 
    cur = db.execute(cur, sql)
    db.db.commit()

    for table in data_sources:
        if table == "entities": continue
        print "Populating has data for", table
        condition = " OR ".join([column + " IS NOT NULL" for column in data_sources[table]])

        sql = "UPDATE entities " + \
            "JOIN " + table + " ON entities.id = " + table + ".id " + \
            "SET entities.has_data=true " + \
            "WHERE " + condition
        cur = db.execute(cur, sql)
        db.db.commit()

def loadFreeQuota():
  api_limit = 99500
  while api_calls < api_limit:
      log("API calls: " + str(api_calls))
      # Bulk process many rows in tables, so that we have about 40 queries remaining or do at least one row
      limit = int((api_limit - api_calls) / 2)
      if limit < 1: limit = 1
      api_calls_before = api_calls
      process(limit)
      if (api_calls_before == api_calls):
          log("Stopping, didn't make any API calls") 

  processRelated()
  log("API calls: " + str(api_calls))

if db.args.related:
  log("OnlyComputingRelated")
  processRelated()
elif db.args.populate_has_data:
  log("populate_has_data")
  populateHasData()
else:
  master = IdMaster()
  loadFreeQuota()
  populateHasData()

log("API Calls: " + str(api_calls))