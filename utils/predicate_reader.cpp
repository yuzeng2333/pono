#include "utils/predicate_reader.h"

#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>

#include "smt-switch/smt.h"
#include "utils/exceptions.h"
#include "utils/logger.h"

using namespace smt;
using namespace std;

namespace pono {

// Minimal JSON helpers for the specific predicate format.
// Handles: [ { "key": "value", ... }, ... ]

// Skip whitespace in a string starting at pos.
static void skip_ws(const string & s, size_t & pos)
{
  while (pos < s.size() && isspace(s[pos])) pos++;
}

// Expect and consume a specific character.
static void expect_char(const string & s, size_t & pos, char c)
{
  skip_ws(s, pos);
  if (pos >= s.size() || s[pos] != c) {
    throw PonoException("predicate_reader: expected '" + string(1, c)
                        + "' at position " + to_string(pos));
  }
  pos++;
}

// Parse a JSON string value (assumes pos is at the opening quote).
static string parse_string(const string & s, size_t & pos)
{
  skip_ws(s, pos);
  if (pos >= s.size() || s[pos] != '"') {
    throw PonoException("predicate_reader: expected '\"' at position "
                        + to_string(pos));
  }
  pos++;  // skip opening quote
  string result;
  while (pos < s.size() && s[pos] != '"') {
    if (s[pos] == '\\') {
      pos++;
      if (pos < s.size()) result += s[pos];
    } else {
      result += s[pos];
    }
    pos++;
  }
  if (pos >= s.size()) {
    throw PonoException("predicate_reader: unterminated string");
  }
  pos++;  // skip closing quote
  return result;
}

// Skip a JSON value (string, number, object, array, bool, null).
static void skip_value(const string & s, size_t & pos)
{
  skip_ws(s, pos);
  if (pos >= s.size()) return;
  char c = s[pos];
  if (c == '"') {
    parse_string(s, pos);
  } else if (c == '{') {
    pos++;
    skip_ws(s, pos);
    if (pos < s.size() && s[pos] == '}') { pos++; return; }
    while (true) {
      parse_string(s, pos);
      expect_char(s, pos, ':');
      skip_value(s, pos);
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ',') { pos++; continue; }
      break;
    }
    expect_char(s, pos, '}');
  } else if (c == '[') {
    pos++;
    skip_ws(s, pos);
    if (pos < s.size() && s[pos] == ']') { pos++; return; }
    while (true) {
      skip_value(s, pos);
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ',') { pos++; continue; }
      break;
    }
    expect_char(s, pos, ']');
  } else {
    // number, bool, null — skip until delimiter
    while (pos < s.size() && s[pos] != ',' && s[pos] != '}' && s[pos] != ']'
           && !isspace(s[pos])) {
      pos++;
    }
  }
}

// Look up an operand name in the transition system.
// Returns the term if found as a named term or state variable symbol.
// If not found and the name looks like an integer constant, creates one.
static Term resolve_operand(const string & name,
                            const TransitionSystem & ts,
                            const SmtSolver & solver)
{
  // First try named_terms (BTOR2 symbol names)
  const auto & named = ts.named_terms();
  auto it = named.find(name);
  if (it != named.end()) {
    return it->second;
  }

  // Also check state variables by their string representation
  for (const auto & sv : ts.statevars()) {
    if (sv->to_string() == name) {
      return sv;
    }
  }

  // Try to parse as an integer constant
  // Need a sort to create the constant — but we don't know the sort yet.
  // Return nullptr and let the caller handle it.
  return nullptr;
}

// --- Simple SMT-LIB2 expression parser for predicate formulas ---

// Forward declaration
static Term parse_smt2_expr(const string & s,
                            size_t & pos,
                            const TransitionSystem & ts,
                            const SmtSolver & solver);

// Parse an SMT2 token (symbol, numeral, etc.)
static string parse_smt2_token(const string & s, size_t & pos)
{
  skip_ws(s, pos);
  string tok;
  while (pos < s.size() && s[pos] != ' ' && s[pos] != '('
         && s[pos] != ')' && !isspace(s[pos])) {
    tok += s[pos++];
  }
  return tok;
}

// Parse an indexed operator like (_ sign_extend 32) or (_ bv0 32)
static Term parse_smt2_indexed(const string & s,
                               size_t & pos,
                               const TransitionSystem & ts,
                               const SmtSolver & solver)
{
  skip_ws(s, pos);
  string op = parse_smt2_token(s, pos);

  if (op == "sign_extend" || op == "zero_extend") {
    skip_ws(s, pos);
    string nstr = parse_smt2_token(s, pos);
    int n = stoi(nstr);
    skip_ws(s, pos);
    if (s[pos] == ')') pos++;  // close the (_ ...)

    // Now parse the argument
    skip_ws(s, pos);
    Term arg = parse_smt2_expr(s, pos, ts, solver);
    if (!arg) return nullptr;

    Sort arg_sort = arg->get_sort();
    int orig_width = arg_sort->get_width();
    Sort result_sort = solver->make_sort(BV, orig_width + n);

    if (op == "sign_extend") {
      return solver->make_term(Op(Sign_Extend, n), arg);
    } else {
      return solver->make_term(Op(Zero_Extend, n), arg);
    }
  } else if (op.substr(0, 2) == "bv") {
    // (_ bvN W) — bitvector constant
    string valstr = op.substr(2);
    skip_ws(s, pos);
    string wstr = parse_smt2_token(s, pos);
    int width = stoi(wstr);
    skip_ws(s, pos);
    if (pos < s.size() && s[pos] == ')') pos++;  // close the (_ ...)

    Sort bvsort = solver->make_sort(BV, width);
    long val = stol(valstr);
    return solver->make_term(val, bvsort);
  }

  return nullptr;
}

// Parse a full SMT2 expression recursively
static Term parse_smt2_expr(const string & s,
                            size_t & pos,
                            const TransitionSystem & ts,
                            const SmtSolver & solver)
{
  skip_ws(s, pos);
  if (pos >= s.size()) return nullptr;

  if (s[pos] == '(') {
    pos++;  // consume '('
    skip_ws(s, pos);

    // Check for indexed operators: (_ ...)
    if (s[pos] == '_') {
      pos++;  // consume '_'
      skip_ws(s, pos);
      // Peek: is this a standalone (_ bvN W) or (_ sign_extend N)?
      // Save position to check
      size_t save = pos;
      string op = parse_smt2_token(s, pos);

      if (op == "sign_extend" || op == "zero_extend") {
        skip_ws(s, pos);
        string nstr = parse_smt2_token(s, pos);
        int n = stoi(nstr);
        skip_ws(s, pos);
        if (pos < s.size() && s[pos] == ')') pos++;  // close (_ ...)
        skip_ws(s, pos);
        Term arg = parse_smt2_expr(s, pos, ts, solver);
        skip_ws(s, pos);
        if (pos < s.size() && s[pos] == ')') pos++;  // close outer
        if (!arg) return nullptr;
        if (op == "sign_extend") {
          return solver->make_term(Op(Sign_Extend, n), arg);
        } else {
          return solver->make_term(Op(Zero_Extend, n), arg);
        }
      } else if (op.substr(0, 2) == "bv") {
        // (_ bvN W)
        string valstr = op.substr(2);
        skip_ws(s, pos);
        string wstr = parse_smt2_token(s, pos);
        int width = stoi(wstr);
        skip_ws(s, pos);
        if (pos < s.size() && s[pos] == ')') pos++;  // close (_ ...)
        Sort bvsort = solver->make_sort(BV, width);
        long val = stol(valstr);
        return solver->make_term(val, bvsort);
      } else {
        // Unknown indexed, restore
        pos = save;
        return nullptr;
      }
    }

    // Check for indexed operator application: ((_ sign_extend 32) arg)
    if (s[pos] == '(') {
      size_t save_outer = pos;
      pos++;  // consume inner '('
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == '_') {
        pos++;  // consume '_'
        skip_ws(s, pos);
        string iop = parse_smt2_token(s, pos);

        if (iop == "sign_extend" || iop == "zero_extend") {
          skip_ws(s, pos);
          string nstr = parse_smt2_token(s, pos);
          int n = stoi(nstr);
          skip_ws(s, pos);
          if (pos < s.size() && s[pos] == ')') pos++;  // close (_ ...)
          skip_ws(s, pos);
          Term arg = parse_smt2_expr(s, pos, ts, solver);
          skip_ws(s, pos);
          if (pos < s.size() && s[pos] == ')') pos++;  // close outer (...)
          if (!arg) return nullptr;
          if (iop == "sign_extend") {
            return solver->make_term(Op(Sign_Extend, n), arg);
          } else {
            return solver->make_term(Op(Zero_Extend, n), arg);
          }
        } else if (iop == "extract") {
          skip_ws(s, pos);
          string histr = parse_smt2_token(s, pos);
          skip_ws(s, pos);
          string lostr = parse_smt2_token(s, pos);
          int hi = stoi(histr);
          int lo = stoi(lostr);
          skip_ws(s, pos);
          if (pos < s.size() && s[pos] == ')') pos++;  // close (_ ...)
          skip_ws(s, pos);
          Term arg = parse_smt2_expr(s, pos, ts, solver);
          skip_ws(s, pos);
          if (pos < s.size() && s[pos] == ')') pos++;  // close outer
          if (!arg) return nullptr;
          return solver->make_term(Op(Extract, hi, lo), arg);
        } else {
          // Unknown indexed op, restore
          pos = save_outer;
        }
      } else {
        // Not an indexed op, restore
        pos = save_outer;
      }
    }

    // Parse operator
    string op = parse_smt2_token(s, pos);

    // Determine operation and arity
    if (op == "=" || op == "bvadd" || op == "bvsub" || op == "bvmul"
        || op == "bvand" || op == "bvor" || op == "bvxor" || op == "bvult"
        || op == "bvule" || op == "bvslt" || op == "bvsle" || op == "bvudiv"
        || op == "bvurem" || op == "bvsrem" || op == "bvshl" || op == "bvlshr"
        || op == "bvashr" || op == "and" || op == "or" || op == "=>") {
      skip_ws(s, pos);
      Term arg1 = parse_smt2_expr(s, pos, ts, solver);
      skip_ws(s, pos);
      Term arg2 = parse_smt2_expr(s, pos, ts, solver);
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ')') pos++;

      if (!arg1 || !arg2) return nullptr;

      PrimOp prim;
      if (op == "=") prim = Equal;
      else if (op == "bvadd") prim = BVAdd;
      else if (op == "bvsub") prim = BVSub;
      else if (op == "bvmul") prim = BVMul;
      else if (op == "bvand") prim = BVAnd;
      else if (op == "bvor") prim = BVOr;
      else if (op == "bvxor") prim = BVXor;
      else if (op == "bvult") prim = BVUlt;
      else if (op == "bvule") prim = BVUle;
      else if (op == "bvslt") prim = BVSlt;
      else if (op == "bvsle") prim = BVSle;
      else if (op == "bvudiv") prim = BVUdiv;
      else if (op == "bvurem") prim = BVUrem;
      else if (op == "bvsrem") prim = BVSrem;
      else if (op == "bvshl") prim = BVShl;
      else if (op == "bvlshr") prim = BVLshr;
      else if (op == "bvashr") prim = BVAshr;
      else if (op == "and") prim = And;
      else if (op == "or") prim = Or;
      else if (op == "=>") prim = Implies;
      else return nullptr;

      return solver->make_term(prim, arg1, arg2);
    } else if (op == "not" || op == "bvnot" || op == "bvneg") {
      skip_ws(s, pos);
      Term arg = parse_smt2_expr(s, pos, ts, solver);
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ')') pos++;
      if (!arg) return nullptr;

      PrimOp prim;
      if (op == "not") prim = Not;
      else if (op == "bvnot") prim = BVNot;
      else prim = BVNeg;

      return solver->make_term(prim, arg);
    } else if (op == "ite") {
      skip_ws(s, pos);
      Term cond = parse_smt2_expr(s, pos, ts, solver);
      skip_ws(s, pos);
      Term then_t = parse_smt2_expr(s, pos, ts, solver);
      skip_ws(s, pos);
      Term else_t = parse_smt2_expr(s, pos, ts, solver);
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ')') pos++;
      if (!cond || !then_t || !else_t) return nullptr;
      return solver->make_term(Ite, cond, then_t, else_t);
    } else {
      // Unknown operator
      return nullptr;
    }
  } else {
    // Atom: variable name or numeral
    string tok = parse_smt2_token(s, pos);
    if (tok.empty()) return nullptr;

    // Try as variable
    Term t = resolve_operand(tok, ts, solver);
    if (t) return t;

    // Unknown atom
    return nullptr;
  }
}

TermVec read_predicates(const string & json_path,
                        const TransitionSystem & ts,
                        const SmtSolver & solver)
{
  // Read the entire file
  ifstream ifs(json_path);
  if (!ifs.is_open()) {
    throw PonoException("Cannot open predicate file: " + json_path);
  }
  stringstream ss;
  ss << ifs.rdbuf();
  string content = ss.str();

  TermVec predicates;
  size_t pos = 0;

  // Parse top-level array
  expect_char(content, pos, '[');

  skip_ws(content, pos);
  if (pos < content.size() && content[pos] == ']') {
    pos++;
    return predicates;  // empty array
  }

  while (true) {
    // Parse one predicate object
    expect_char(content, pos, '{');

    string pred_type;
    string op1_name;
    string op2_name;

    // Parse key-value pairs
    skip_ws(content, pos);
    while (pos < content.size() && content[pos] != '}') {
      string key = parse_string(content, pos);
      expect_char(content, pos, ':');

      if (key == "predicate_type") {
        pred_type = parse_string(content, pos);
      } else if (key == "operands") {
        // Parse operands sub-object
        expect_char(content, pos, '{');
        skip_ws(content, pos);
        while (pos < content.size() && content[pos] != '}') {
          string okey = parse_string(content, pos);
          expect_char(content, pos, ':');
          string oval = parse_string(content, pos);
          if (okey == "op1") op1_name = oval;
          else if (okey == "op2") op2_name = oval;
          skip_ws(content, pos);
          if (pos < content.size() && content[pos] == ',') pos++;
          skip_ws(content, pos);
        }
        expect_char(content, pos, '}');
      } else {
        // Skip unknown fields (btor2_info, predicate_id, etc.)
        skip_value(content, pos);
      }

      skip_ws(content, pos);
      if (pos < content.size() && content[pos] == ',') pos++;
      skip_ws(content, pos);
    }
    expect_char(content, pos, '}');

    // Handle smt2 predicate type: parse SMT-LIB2 formula from op1
    if (pred_type == "smt2") {
      if (op1_name.empty()) {
        logger.log(0, "WARNING: smt2 predicate missing formula in op1, skipping");
      } else {
        size_t smt_pos = 0;
        Term pred = parse_smt2_expr(op1_name, smt_pos, ts, solver);
        if (pred) {
          predicates.push_back(pred);
          logger.log(1, "Loaded smt2 predicate: {}", pred->to_string());
        } else {
          logger.log(0, "WARNING: failed to parse smt2 formula '{}', skipping",
                     op1_name);
        }
      }
      goto next_pred;
    }

    // Construct the predicate term (simple binary predicate types)
    if (op1_name.empty()) {
      logger.log(0, "WARNING: predicate missing op1, skipping");
    } else {
      Term op1 = resolve_operand(op1_name, ts, solver);
      if (!op1) {
        logger.log(0, "WARNING: cannot resolve operand '{}', skipping predicate",
                   op1_name);
      } else {
        Term op2 = nullptr;
        if (!op2_name.empty()) {
          op2 = resolve_operand(op2_name, ts, solver);
          if (!op2) {
            // Try parsing as integer constant with op1's sort
            try {
              long val = stol(op2_name);
              op2 = solver->make_term(val, op1->get_sort());
            } catch (const exception &) {
              logger.log(0,
                         "WARNING: cannot resolve operand '{}', "
                         "skipping predicate",
                         op2_name);
            }
          }
        }

        if (op2) {
          PrimOp prim_op;
          if (pred_type == "eq") {
            prim_op = Equal;
          } else if (pred_type == "neq") {
            prim_op = Distinct;
          } else if (pred_type == "lt" || pred_type == "ult") {
            prim_op = BVUlt;
          } else if (pred_type == "lte" || pred_type == "ule") {
            prim_op = BVUle;
          } else if (pred_type == "uge") {
            prim_op = BVUge;
          } else {
            logger.log(
                0, "WARNING: unknown predicate_type '{}', skipping", pred_type);
            goto next_pred;
          }
          Term pred = solver->make_term(prim_op, op1, op2);
          predicates.push_back(pred);
          logger.log(1, "Loaded predicate: {}", pred->to_string());
        }
      }
    }

  next_pred:
    skip_ws(content, pos);
    if (pos < content.size() && content[pos] == ',') {
      pos++;
    } else {
      break;
    }
  }

  expect_char(content, pos, ']');

  return predicates;
}

}  // namespace pono