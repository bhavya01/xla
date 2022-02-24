#include "torch_xla/csrc/ops/nll_loss2d_backward.h"

#include "tensorflow/compiler/xla/xla_client/debug_macros.h"
#include "tensorflow/compiler/xla/xla_client/util.h"
#include "torch/csrc/lazy/core/ir.h"
#include "torch_xla/csrc/lowering_context.h"
#include "torch_xla/csrc/nll_loss.h"
#include "torch_xla/csrc/ops/infer_output_shape.h"

namespace torch_xla {
namespace ir {
namespace ops {
namespace {

xla::Shape NodeOutputShape(const Value& grad_output, const Value& logits,
                           const Value& labels,
                           const absl::optional<Value>& weight,
                           const absl::optional<Value>& total_weight,
                           ReductionMode reduction, int ignore_index) {
  auto lower_for_shape_fn =
      [&](absl::Span<const xla::XlaOp> operands) -> xla::XlaOp {
    xla::XlaOp weight;
    xla::XlaOp total_weight;
    if (operands.size() > 3) {
      XLA_CHECK_EQ(operands.size(), 5)
          << "If weight is specified, so must be total_weight";
      weight = operands[3];
      total_weight = operands[4];
    }
    return BuildNllLossBackward(operands[0], operands[1], operands[2], weight,
                                total_weight, ignore_index, reduction);
  };
  std::vector<xla::Shape> shapes;
  for (auto& input : xla::util::GetValuesVector<Value>(
           {grad_output, logits, labels}, {&weight, &total_weight})) {
    shapes.push_back(input.shape());
  }
  return InferOutputShape(shapes, lower_for_shape_fn);
}

}  // namespace

NllLoss2dBackward::NllLoss2dBackward(const Value& grad_output,
                                     const Value& logits, const Value& labels,
                                     const absl::optional<Value>& weight,
                                     const absl::optional<Value>& total_weight,
                                     ReductionMode reduction, int ignore_index)
    : Node(
          torch::lazy::OpKind(at::aten::nll_loss2d_backward),
          xla::util::GetValuesVector<Value>({grad_output, logits, labels},
                                            {&weight, &total_weight}),
          [&]() {
            return NodeOutputShape(grad_output, logits, labels, weight,
                                   total_weight, reduction, ignore_index);
          },
          /*num_outputs=*/1,
          torch::lazy::MHash(xla::util::GetEnumValue(reduction), ignore_index)),
      reduction_(reduction),
      ignore_index_(ignore_index) {}

NodePtr NllLoss2dBackward::Clone(OpList operands) const {
  absl::optional<Value> weight;
  absl::optional<Value> total_weight;
  if (operands.size() > 3) {
    weight = operands.at(3);
    total_weight = operands.at(4);
  }
  return ir::MakeNode<NllLoss2dBackward>(operands.at(0), operands.at(1),
                                     operands.at(2), weight, total_weight,
                                     reduction_, ignore_index_);
}

XlaOpVector NllLoss2dBackward::Lower(LoweringContext* loctx) const {
  xla::XlaOp grad_output = loctx->GetOutputOp(operand_with_shape(0));
  xla::XlaOp logits = loctx->GetOutputOp(operand_with_shape(1));
  xla::XlaOp labels = loctx->GetOutputOp(operand_with_shape(2));
  xla::XlaOp weight;
  xla::XlaOp total_weight;
  if (operands_with_shape().size() > 3) {
    weight = loctx->GetOutputOp(operand_with_shape(3));
    total_weight = loctx->GetOutputOp(operand_with_shape(4));
  }
  return ReturnOp(BuildNllLossBackward(grad_output, logits, labels, weight,
                                       total_weight, ignore_index_, reduction_),
                  loctx);
}

std::string NllLoss2dBackward::ToString() const {
  std::stringstream ss;
  ss << Node::ToString()
     << ", reduction=" << xla::util::GetEnumValue(reduction_)
     << ", ignore_index=" << ignore_index_;
  return ss.str();
}

}  // namespace ops
}  // namespace ir
}  // namespace torch_xla
