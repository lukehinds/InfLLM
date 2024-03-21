import torch
from typing import Optional
from copy import deepcopy
from .dot_production_attention import get_multi_stage_dot_production_attention

class TransferingTensor:
    def __init__(self, tensor, to_cpu: bool):
        if isinstance(tensor, TransferingTensor):
            tensor = tensor.get()

        assert isinstance(tensor, torch.Tensor)
            
        if to_cpu:
            assert tensor.is_cuda
            self.is_cuda = False
        else:
            assert not tensor.is_cuda
            self.is_cuda = True
        
        self.tensor = tensor.to(
            device="cpu" if to_cpu else "cuda",
            non_blocking=True
        )
        self.event = torch.cuda.Event()
        self.event.record()

    def get(self):
        self.event.wait()
        return self.tensor

    def __len__(self):
        return len(self.tensor)


    def __getattr__(self, name):
        self.event.wait()
        return getattr(self.tensor, name)


class VectorTensor:
    def __init__(
        self, 
        element_shape,
        element_dtype,
        concat_dim,
    ):
        init_cached_size = 16
        if concat_dim != 0:
            element_shape = (element_shape[concat_dim],) + element_shape[1:concat_dim] + (element_shape[0],)  + element_shape[concat_dim+1:]
        init_data_shape = (init_cached_size,) + element_shape[1:]
        self.concat_dim = concat_dim
        self.data = torch.empty(
            init_data_shape,
            dtype=element_dtype,
            device='cuda'
        )
        self.length = 0
        self.cache_size = init_cached_size

    def append_cache(self):
        new_cache_size = self.cache_size * 2
        data_shape = self.data.shape
        new_data = torch.empty(
            (new_cache_size,) + data_shape[1:],
            device='cuda',
            dtype=self.data.dtype
        )
        new_data[:self.cache_size,...].copy_(self.data)
        self.data = new_data
        self.cache_size = new_cache_size

    def append(self, tensor: torch.Tensor):
        assert tensor.dtype == self.data.dtype
        assert tensor.is_contiguous()
        if self.concat_dim != 0:
            tensor = tensor.transpose(0, self.concat_dim)

        append_l = tensor.size(0)

        while self.length + append_l > self.cache_size:
            self.append_cache()

        self.data[self.length: self.length+append_l, ...].copy_(tensor)

        self.length += append_l


    def get_data(self):
        if self.concat_dim == 0:
            return self.data[:self.length, ...]

        return self.data[:self.length,...].transpose(0, self.concat_dim)


    def __len__(self):
        return self.length

GLOBAL_STREAM = None


class ContextManager:
    def __init__(self, 
                 position_embedding,
                 n_init, n_local, 
                 block_size, max_cached_block, topk, exc_block_size, 
                 perhead = False,
                 score_decay: float = 0.1, fattn: bool = False,
                 repr_topk: int = 1,
                 max_calc_block: Optional[int] = None,
                 use_buffer = True,
                 cache_strategy = "lru",
                 calc_block_score = False,
                 ignore_remainder: bool = False,
                 chunk_topk_calc: Optional[int] = None,
                 async_global_stream: bool = True
    ):
        if max_calc_block is None:
            max_calc_block = topk

        assert max_calc_block >= topk
        self.max_calc_block = max_calc_block
        self.length = 0
        self.position_embedding = position_embedding
        self.n_init = n_init
        self.n_local = n_local
        self.block_size = block_size
        self.max_cached_block = max_cached_block
        self.exc_block_size = exc_block_size
        self.score_decay = score_decay
        assert exc_block_size <= n_local # no global token in input
        self.topk = topk
        self.Attn, _ = get_multi_stage_dot_production_attention(fattn)
        self.fattn = fattn
        self.initialized = False
        self.perhead = perhead
        self.repr_topk = repr_topk
        self.use_buffer = use_buffer
        self.cache_strategy = cache_strategy
        self.calc_block_score = calc_block_score
        self.load_count = 0
        self.ignore_remainder = ignore_remainder
        self.chunk_topk_calc = chunk_topk_calc
        self.async_global_stream = async_global_stream

        global GLOBAL_STREAM
        if self.async_global_stream and GLOBAL_STREAM is None:
            GLOBAL_STREAM = torch.cuda.Stream()
            

        assert cache_strategy in ["lru", "fifo", "lru-s"]

        if cache_strategy == "lru-s":
            assert calc_block_score, "Block score calcualtion is needed for LRU-S cache strategy."

        
    def load_block(self, b, i):
        if i in self.cached_blocks[b]:
            if self.cache_strategy == "lru":
                self.cached_blocks[b][i] = self.load_count
                self.load_count += 1
            assert self.global_blocks[b][i][0].is_cuda
            assert self.global_blocks[b][i][1].is_cuda
            return False

        self.global_blocks[b][i] = (
            TransferingTensor(self.global_blocks[b][i][0], False),
            TransferingTensor(self.global_blocks[b][i][1], False)
        )
        if self.cache_strategy in ["fifo", "lru"]:
            self.cached_blocks[b][i] = self.load_count
            self.load_count += 1
        else:
            self.cached_blocks[b][i] = 0

        return True


    def offload_block(self, u, i):
        if i not in self.cached_blocks[u]:
            return False
        self.global_blocks[u][i] = (
            TransferingTensor(self.global_blocks[u][i][0], True),
            TransferingTensor(self.global_blocks[u][i][1], True),
        )
        self.cached_blocks[u].pop(i)
        return True


    def remove_lru_blocks(self):
        for u in range(self.num_units):
            if len(self.cached_blocks[u]) <= self.max_cached_block:
                continue

            num_remove = len(self.cached_blocks[u]) - self.max_cached_block
            lst = list(self.cached_blocks[u].items())
            lst.sort(key=lambda x: x[1])

            for i in range(num_remove):
                assert self.offload_block(u, lst[i][0])


    def get_block_k(self, k, score):
        assert isinstance(score, torch.Tensor)
        assert k.dim() >= 2
        assert k.shape[:-1] == score.shape
        assert k.shape[-2] == self.block_size
        score_topk = score.topk(self.repr_topk, dim=-1).indices
        assert score_topk.shape == (self.num_units, self.unit_size, self.repr_topk)
        ret = torch.gather(k, -2, score_topk[:, :, :, None].expand(self.num_units, self.unit_size, self.repr_topk, self.dim_head))
        return ret

    def flat_to_unit(self, tensor):
        assert tensor.size(0) == self.batch_size
        if tensor.size(1) == self.num_heads:
            return tensor.view((self.num_units, self.unit_size) + tuple(tensor.shape[2:]))
        elif tensor.size(1) == self.num_heads_kv:
            tensor = tensor.view((self.batch_size, self.num_heads_kv, 1) + tuple(tensor.shape[2:]))
            shape = list(tensor.shape)
            shape[2] *= self.num_heads // self.num_heads_kv
            tensor = tensor.expand(tuple(shape))
            tensor = tensor.reshape((self.batch_size, self.num_heads) + tuple(shape[3:]))
            return tensor.view((self.num_units, self.unit_size) + tuple(tensor.shape[2:]))
        else:
            raise ValueError

    def from_group_kv(self, tensor):
        if self.perhead:
            return tensor

        assert tensor.dim() == 3
        assert tensor.size(0) == self.num_heads_kv
        if self.num_heads == self.num_heads_kv:
            return tensor
        _, length, dim_head = tensor.shape
        num_group = self.num_heads // self.num_heads_kv
        tensor = tensor.view((self.num_heads_kv, 1, length, dim_head))
        tensor = tensor.expand((self.num_heads_kv, num_group, length, dim_head)).reshape((self.num_heads, length, dim_head))
        return tensor

            
    def to_group_kv(self, tensor):
        if self.perhead:
            return tensor

        assert tensor.dim() == 3
        assert tensor.size(0) == self.num_heads
        if self.num_heads == self.num_heads_kv:
            return tensor

        num_group = self.num_heads // self.num_heads_kv
        _, length, dim_head = tensor.shape
        tensor = tensor.view((self.num_heads_kv, num_group, length, dim_head))
        tensor = tensor[:, 0, :, :].contiguous()
        return tensor

    def init(
        self, 
        local_q, local_k, local_v,
        global_q, global_k, global_v
    ):
        assert local_q.dim() == 4
        batch_size, num_heads, len_q, dim_head = local_q.shape
        num_heads_kv = local_k.size(1)

        for _t in [local_q, local_k, local_v, global_q, global_k, global_v]:
            assert _t.size(0) == batch_size
            assert (_t.size(1) == num_heads or _t.size(1) == num_heads_kv)
            assert _t.size(2) == len_q
            assert _t.size(3) == dim_head
            assert _t.is_cuda


        self.batch_size = batch_size
        self.num_heads = num_heads
        self.num_heads_kv = num_heads_kv
        self.dim_head = dim_head
        if self.perhead:
            self.num_units = batch_size * num_heads
            self.unit_size = 1
        else:
            self.num_units = batch_size
            self.unit_size = num_heads

        self.global_blocks = [[] for _ in range(self.num_units)] # [[(global_k, global_v)]]
        self.cached_blocks = [{} for _ in range(self.num_units)] # [[block_id: block_score]
        self.num_global_block = 0

        self.block_k = VectorTensor(
            (self.num_units, self.unit_size, -1, dim_head), global_k.dtype, 2
        )
        self.local_k = torch.empty((self.num_units, self.unit_size, 0, dim_head), dtype=local_k.dtype, device=local_k.device)
        self.local_v = torch.empty((self.num_units, self.unit_size, 0, dim_head), dtype=local_v.dtype, device=local_v.device)

        self.global_remainder = (
            torch.empty((self.num_units, self.unit_size, 0, dim_head), dtype=global_k.dtype, device=global_k.device),
            torch.empty((self.num_units, self.unit_size, 0, dim_head), dtype=global_v.dtype, device=global_v.device),
        )

        self.global_remainder_local_score = torch.empty((self.num_units, self.unit_size, 0), dtype=global_k.dtype, device=global_k.device)


        self.init_k = torch.empty((self.num_units, self.unit_size, 0, dim_head), dtype=global_k.dtype, device=global_k.device)
        self.init_v = torch.empty((self.num_units, self.unit_size, 0, dim_head), dtype=global_k.dtype, device=global_k.device)
        self.init_exc = False
        self.dtype = local_q.dtype
        self.position_embedding._update_cos_sin_tables_len(
            self.n_local + self.exc_block_size + 1, local_k.device, local_k.dim()
        )

        if self.use_buffer:
            buffer_len = self.max_calc_block * self.block_size + self.exc_block_size + self.block_size + self.n_init
            if self.ignore_remainder:
                buffer_len -= self.exc_block_size + self.block_size
            self.global_buffer = torch.zeros(
                    (2, self.num_units, self.unit_size, buffer_len , dim_head),
                    dtype = global_k.dtype, device=global_k.device
                )
            self.global_buffer_block_id_list = [[-1] * self.max_calc_block for _ in range(self.num_units)]
            self.global_buffer_init_st = 0
            self.global_buffer_init_ed = 0

        self.initialized = True
    

    def calc_block_topk(
        self, global_h_q
    ):
        if not self._use_chunk_topk:
            if self.num_global_block <= self.topk:
                return [list(range(len(self.global_blocks[0]))) for _ in range(self.num_units)]

            block_k = self.block_k.get_data()
            assert block_k.shape == (self.num_units, self.unit_size, self.num_global_block, self.dim_head)

            global_h_q = global_h_q.mean(dim=2, keepdim=True)
            assert global_h_q.shape == (self.num_units, self.unit_size, 1, self.dim_head)

            block_score = torch.matmul(
                global_h_q, block_k.transpose(-1, -2)
            ) # (num_units, unit_size, 1, num_global_block * repr_topk)

            block_score = block_score.squeeze(dim=2)
            block_score = block_score.mean(dim=1) 

            assert block_score.shape == (self.num_units, self.num_global_block)
            indices = block_score.topk(self.topk, dim=-1).indices.cpu()
            assert indices.shape == (self.num_units, self.topk)

            ret = []
            for u in range(self.num_units):
                ret.append(indices[u].tolist())
        
        else:
            return self._cached_topk[self._topk_cur]

        return ret


    def get_global_hidden_and_mask(
        self, len_q, block_topk
    ):
        assert len(block_topk) == self.num_units
        global_block_map = [[] for _ in range(self.num_units)]
        global_remainder_len = max(self._global_remainder_ed - self._global_remainder_st + len_q - self.n_local, 0)
        init_len = self.init_k.size(-2)
        sliding_window = None

        total_len = self.max_calc_block * self.block_size + self.exc_block_size + self.block_size + self.n_init
        if self.ignore_remainder and self.init_exc:
            total_len -= self.exc_block_size + self.block_size

        non_blocking_copy = True
        
        if self.use_buffer:
            global_h_k = self.global_buffer[0]
            global_h_v = self.global_buffer[1]
        else:
            global_h_k = torch.empty(
                (self.num_units, self.unit_size, total_len, self.dim_head),
                device='cuda', dtype=self.dtype
            )
            global_h_v = torch.zeros_like(global_h_k)

        block_num = None
        for u in range(self.num_units):
            block_score = []
            for k, s in self.cached_blocks[u].items():
                if k in block_topk[u]:
                    block_score.append((k, float("inf")))
                else:
                    block_score.append((k, min(s, 1e8)))

            block_score.sort(key=lambda x: x[1], reverse=True)
            block_score = block_score[ :self.max_calc_block]

            if block_num is None:
                block_num = len(block_score)
            else:
                # calc block num should be the same for all units
                assert block_num == len(block_score)
            
            st = 0
            ed = 0
            global_block_map[u] = [-1] * block_num
            if self.use_buffer:
                global_block_map[u] = deepcopy(self.global_buffer_block_id_list[u])


            b_idx_list = [block_score[i][0] for i in range(block_num)]
            for b_idx in b_idx_list:
                if b_idx in global_block_map[u]:
                    continue

                st = -1
                ed = -1
                for j in range(self.max_calc_block):
                    if global_block_map[u][j] == -1 or global_block_map[u][j] not in b_idx_list:
                        st = j * self.block_size
                        ed = st + self.block_size
                        global_block_map[u][j] = b_idx
                        break

                global_h_k[u, :, st:ed, :].copy_(self.from_group_kv(self.global_blocks[u][b_idx][0].get()), non_blocking=non_blocking_copy)
                global_h_v[u, :, st:ed, :].copy_(self.from_group_kv(self.global_blocks[u][b_idx][1].get()), non_blocking=non_blocking_copy)

             
        init_st = block_num * self.block_size
        init_ed = init_st + init_len
        if (not self.use_buffer) or self.global_buffer_init_st != init_st or self.global_buffer_init_ed != init_ed:
            global_h_k[:, :, init_st: init_ed, :].copy_(self.init_k, non_blocking=non_blocking_copy)
            global_h_v[:, :, init_st: init_ed, :].copy_(self.init_v, non_blocking=non_blocking_copy)

        ed = init_ed

        if not self.ignore_remainder or init_len < self.n_init:
            rmd_st = init_ed
            rmd_ed = rmd_st + global_remainder_len
            ed = rmd_ed
            global_h_k[:, :, rmd_st: rmd_ed, :].copy_(self.global_remainder[0][:, :, self._global_remainder_st:self._global_remainder_st+global_remainder_len, :], non_blocking=non_blocking_copy)
            global_h_v[:, :, rmd_st: rmd_ed, :].copy_(self.global_remainder[1][:, :, self._global_remainder_st:self._global_remainder_st+global_remainder_len, :], non_blocking=non_blocking_copy)


            sliding_window = (self.global_remainder[0].size(-2) + rmd_st, self.n_local)

        if self.use_buffer:
            self.global_buffer_block_id_list = deepcopy(global_block_map)
            self.global_buffer_init_st = init_st
            self.global_buffer_init_ed = init_ed

        for u in range(self.num_units):
            assert max(global_block_map[u][block_num:] + [-1]) == -1
            assert min(global_block_map[u][:block_num] + [0]) > -1
            global_block_map[u] = list(global_block_map[u][:block_num])


        global_h_k = global_h_k[:, :, :ed, :]
        global_h_v = global_h_v[:, :, :ed, :]
        return global_h_k, global_h_v, sliding_window, global_block_map, block_num


    def update_block_score(
        self, global_score: torch.FloatTensor, global_block_map, global_block_num
    ):
        if global_score is not None:
            global_score = global_score[:, :, :, :global_block_num * self.block_size].mean(dim=-2)
            assert global_score.shape == (self.num_units, self.unit_size, global_block_num * self.block_size)
            global_score = global_score.view(self.num_units, self.unit_size, global_block_num, self.block_size)
            global_score = global_score.sum(dim=-1).sum(dim=1)
            assert global_score.shape == (self.num_units, global_block_num)
            global_score = global_score.to(device='cpu', non_blocking=False) # (num_units, global_block_num)
            for u in range(self.num_units):
                for k, v in self.cached_blocks[u].items():
                    self.cached_blocks[u][k] = v * self.score_decay
                score = global_score[u].tolist()
                assert len(score) >= len(global_block_map[u])
                for s, i in zip(score, global_block_map[u]):
                    self.cached_blocks[u][i] += s


    
    def _append(
        self,
        local_q, local_k, local_v, global_q
    ):

        # get local_h_q, local_h_k, local_h_v
        local_h_q, local_h_k = self.position_embedding(local_q, local_k)
        local_h_v = local_v


        # calc local result first to overlap host-device communication
        attn = self.Attn(local_h_q.shape, local_h_q.dtype, local_h_q.device)
        attn.append(
            local_h_q, local_h_k, local_h_v, 
            get_score=True, sliding_window=self.n_local
        )

        # calc topk global repr k and load cache
        with torch.cuda.stream(GLOBAL_STREAM):
            block_topk = self.calc_block_topk(global_q)

            for u in range(self.num_units):
                for i in block_topk[u]:
                    self.load_block(u, i)

            # get global_h_k, global_h_v, global_mask
            #    Beacuse exc_block_size <= n_local, no global_k, global_v used in global part
            global_h_q = global_q
            global_h_k, global_h_v, global_sliding_window, global_block_map, global_block_num = self.get_global_hidden_and_mask(local_h_q.size(-2), block_topk)

        if self.async_global_stream:
            torch.cuda.current_stream().wait_stream(GLOBAL_STREAM)
        
        # calc global result
        attn.append(
            global_h_q, global_h_k, global_h_v, 
            end=True, get_score=self.calc_block_score, 
            sliding_window=global_sliding_window,
            complement_sliding_window=True
        )

        o, score_list = attn.get_result()
        loc_score = score_list[0]
        glb_score = score_list[1]

        if self.cache_strategy != "lru-s":
            with torch.cuda.stream(GLOBAL_STREAM):
                self.remove_lru_blocks()

        if self.async_global_stream:
            GLOBAL_STREAM.wait_stream(torch.cuda.current_stream())

        # update global score
        with torch.cuda.stream(GLOBAL_STREAM):
            self.update_block_score(glb_score, global_block_map, global_block_num)
        
        # update cache
        if self.cache_strategy == "lru-s":
            with torch.cuda.stream(GLOBAL_STREAM):
                self.remove_lru_blocks()


        return o.view((self.batch_size, self.num_heads, -1, self.dim_head)), loc_score


    def get_batched_topk(self, global_q):
        length = global_q.shape[2]
        exc_num = (length + self.exc_block_size - 1) // self.exc_block_size
        exc_block_num = length // self.exc_block_size
        ret = []
        if self.num_global_block <= self.topk:
            for _ in range(exc_num):
                ret.append(
                    [list(range(len(self.global_blocks[0]))) for _ in range(self.num_units)]
                )
            return ret


        global_q = self.flat_to_unit(global_q)
        global_h_q = global_q
        assert global_h_q.dim() == 4
        assert global_h_q.shape[:2] == (self.num_units, self.unit_size)
        assert global_h_q.shape[3] == self.dim_head



        block_k = self.block_k.get_data()
        assert block_k.shape == (self.num_units, self.unit_size, self.num_global_block, self.dim_head)


        if exc_block_num > 0:
            tmp_global_h_q = global_h_q[:, :, :exc_block_num * self.exc_block_size, :].reshape(
                self.num_units, self.unit_size, exc_block_num, self.exc_block_size, self.dim_head
            ).mean(dim=-2)
            assert tmp_global_h_q.shape == (self.num_units, self.unit_size, exc_block_num, self.dim_head)
            block_score = torch.matmul(
                tmp_global_h_q, block_k.transpose(-1, -2)
            ).mean(dim=1) # (num_units, exc_block_num, num_global_block)
            assert block_score.shape == (self.num_units, exc_block_num, self.num_global_block)

            indices = block_score.topk(self.topk, dim=-1).indices.cpu()
            for b in range(exc_block_num):
                tmp = []
                for u in range(self.num_units):
                    tmp.append(indices[u, b].tolist())
                    assert len(tmp[-1]) == self.topk
                
                ret.append(tmp)

        if exc_block_num != exc_num: 
            tmp_global_h_q = global_h_q[:, :, exc_block_num * self.exc_block_size:, :].reshape(
                self.num_units, self.unit_size, length - exc_block_num * self.exc_block_size, self.dim_head
            ).mean(dim=-2, keepdim=True)
            assert tmp_global_h_q.shape == (self.num_units, self.unit_size, 1, self.dim_head)
            block_score = torch.matmul(
                tmp_global_h_q, block_k.transpose(-1, -2)
            )
            assert block_score.shape == (self.num_units, self.unit_size, 1, self.num_global_block)
            block_score = block_score.squeeze(dim=2).mean(dim=1)
            assert block_score.shape == (self.num_units, self.num_global_block)
            indices = block_score.topk(self.topk, dim=-1).indices.cpu()
            tmp = []
            for u in range(self.num_units):
                tmp.append(indices[u].tolist())
                assert len(tmp[-1]) == self.topk

            ret.append(tmp)

         
        return ret

    def append_global(
        self, exc_length, kv_length, local_score
    ):

        global_remainder_ed = self._global_remainder_ed + exc_length
        global_remainder_st = self._global_remainder_st

        global_remainder_len = global_remainder_ed - global_remainder_st

        assert local_score.shape[:3] == (self.num_units, self.unit_size, kv_length)
        local_score = local_score[:, :, -exc_length-self.n_local:]
        self.global_remainder_local_score[:, :, global_remainder_ed-local_score.size(-1):global_remainder_ed].add_(local_score)
        

        if not self.init_exc and global_remainder_len > self.n_local:
            global_k = self.global_remainder[0]
            global_v = self.global_remainder[1]

            append_init_len = min(
                self.n_init - self.init_k.size(-2),
                global_remainder_len - self.n_local
            )
            self.init_k = torch.cat(
                (self.init_k, global_k[:, :, global_remainder_st:global_remainder_st + append_init_len, :]), dim=-2
            )
            self.init_v = torch.cat(
                (self.init_v, global_v[:, :, global_remainder_st:global_remainder_st + append_init_len, :]), dim=-2
            )
            global_remainder_st += append_init_len
            global_remainder_len -= append_init_len

            if self.init_k.size(-2) == self.n_init:
                self.init_exc = True


        while global_remainder_len - self.block_size >= self.n_local:
            global_remainder_len -= self.block_size
            for u in range(self.num_units):
                self.global_blocks[u].append((
                    TransferingTensor(self.to_group_kv(self.global_remainder[0][u, :, global_remainder_st:global_remainder_st + self.block_size, :]), True),
                    TransferingTensor(self.to_group_kv(self.global_remainder[1][u, :, global_remainder_st:global_remainder_st + self.block_size, :]), True)
                ))

            global_block_k = self.get_block_k(
                self.global_remainder[0][:, :, global_remainder_st:global_remainder_st + self.block_size, :],
                self.global_remainder_local_score[:, :, global_remainder_st:global_remainder_st + self.block_size]
            )
            assert global_block_k.shape == (self.num_units, self.unit_size, self.repr_topk, self.dim_head)
            global_block_k = global_block_k.mean(dim=-2, keepdim=True)

            self.num_global_block += 1
            self.block_k.append(global_block_k)
            global_remainder_st += self.block_size

        self._global_remainder_ed = global_remainder_ed
        self._global_remainder_st = global_remainder_st


    def append(
        self,
        local_q, local_k, local_v,
        global_q, global_k, global_v,
    ):
        if not self.initialized:
            self.init(
                local_q, local_k, local_v,
                global_q, global_k, global_v
            )

        input_length = local_q.size(-2)
        
        if self.async_global_stream:
            GLOBAL_STREAM.wait_stream(torch.cuda.current_stream())

        local_q = self.flat_to_unit(local_q)
        local_k = self.flat_to_unit(local_k)
        local_v = self.flat_to_unit(local_v)
        with torch.cuda.stream(GLOBAL_STREAM):
            global_q = self.flat_to_unit(global_q)
            global_k = self.flat_to_unit(global_k)
            global_v = self.flat_to_unit(global_v)

        # append local and global tensor
        self.local_k = torch.cat((self.local_k, local_k), dim=-2)
        self.local_v = torch.cat((self.local_v, local_v), dim=-2)
        kv_length = self.local_k.size(-2)

        # append global remainder
        with torch.cuda.stream(GLOBAL_STREAM):
            self._global_remainder_st = 0
            self._global_remainder_ed = self.global_remainder[0].size(-2)

            self.global_remainder = (
                torch.cat((self.global_remainder[0], global_k), dim=-2),
                torch.cat((self.global_remainder[1], global_v), dim=-2),
            )

            self.global_remainder_local_score = torch.cat(
                (self.global_remainder_local_score, 
                torch.zeros(
                        (self.num_units, self.unit_size, global_k.size(-2)),
                        dtype=global_k.dtype, device=global_k.device
                    )
                ),
                dim=-1
            )


        with torch.cuda.stream(GLOBAL_STREAM):
            global_q = self.position_embedding.apply_rotary_pos_emb_one_angle(
                global_q, self.n_local
            )

        use_chunk_topk = self.chunk_topk_calc is not None and input_length > 1
        self._use_chunk_topk = use_chunk_topk
        if use_chunk_topk:
            exc_block_num = input_length // self.exc_block_size
            exc_block_per_topk_chunk = self.chunk_topk_calc // self.exc_block_size
            calc_cur_list = [i * self.exc_block_size for i in range(0, exc_block_num + 1, exc_block_per_topk_chunk)]
            if calc_cur_list[-1] < input_length:
                calc_cur_list.append(input_length)
            self._topk_cur = 0
            self._topk_calc_cur = -1

        o_list = []

        for st in range(0, input_length, self.exc_block_size): 
            ed = min(st + self.exc_block_size, input_length)
            if use_chunk_topk and calc_cur_list[self._topk_calc_cur + 1] < ed:
                # calculate topk and sync with host here
                assert ed <= calc_cur_list[self._topk_calc_cur + 2]
                self._topk_calc_cur += 1
                with torch.cuda.stream(GLOBAL_STREAM):
                    self._cached_topk = self.get_batched_topk(global_q[:, :, calc_cur_list[self._topk_calc_cur]: calc_cur_list[self._topk_calc_cur + 1], :])
                self._topk_cur = 0

            kv_st = max(kv_length + st - input_length - self.n_local, 0)
            kv_ed = kv_length + ed - input_length
            chunk_o, local_score = self._append(
                local_q[:, :, st:ed, :],
                self.local_k[:, :, kv_st: kv_ed, :],
                self.local_v[:, :, kv_st: kv_ed, :],
                global_q[:, :, st:ed, :]
            )
            o_list.append(chunk_o)


            # append global
            with torch.cuda.stream(GLOBAL_STREAM):
                self.append_global(ed - st, kv_ed - kv_st, local_score)

            if use_chunk_topk:
                self._topk_cur += 1

        self.length += input_length

        # update local and global tensor
        if self.local_k.size(-2) >= self.n_local:
            self.local_k = self.local_k[:, :, -self.n_local:, :]
            self.local_v = self.local_v[:, :, -self.n_local:, :]

        assert self._global_remainder_ed == self.global_remainder[0].size(-2)
        with torch.cuda.stream(GLOBAL_STREAM):
            self.global_remainder = (
                self.global_remainder[0][:, :, self._global_remainder_st:, :],
                self.global_remainder[1][:, :, self._global_remainder_st:, :]
            )
            self.global_remainder_local_score = self.global_remainder_local_score[:, :, self._global_remainder_st:]

        return torch.cat(o_list, dim=-2)


    def size(self, *args, **kwargs):
        return self.length
