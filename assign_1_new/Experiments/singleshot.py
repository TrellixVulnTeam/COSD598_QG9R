import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from Utils import load
from Utils import generator
from Utils import metrics
from train import *
from prune import *

import timeit
import os

def run(args):
    ## Random Seed and Device ##
    torch.manual_seed(args.seed)
    device = load.device(args.gpu)

    ## Data ##
    print('Loading {} dataset.'.format(args.dataset))
    input_shape, num_classes = load.dimension(args.dataset) 
    prune_loader = load.dataloader(args.dataset, args.prune_batch_size, True, args.workers, args.prune_dataset_ratio * num_classes)
    train_loader = load.dataloader(args.dataset, args.train_batch_size, True, args.workers)
    test_loader = load.dataloader(args.dataset, args.test_batch_size, False, args.workers)

    log_filename = '{}/{}'.format(args.result_dir, 'result.log')
    fout = open(log_filename, 'w')
    fout.write('start!\n')

    if args.compression_list == []:
        args.compression_list.append(args.compression)
    if args.pruner_list == []:
        args.pruner_list.append(args.pruner)
    
    ## Model, Loss, Optimizer ##
    print('Creating {}-{} model.'.format(args.model_class, args.model))
    model = load.model(args.model, args.model_class)(input_shape, 
                                                    num_classes, 
                                                    args.dense_classifier, 
                                                    args.pretrained).to(device)
    loss = nn.CrossEntropyLoss()
    opt_class, opt_kwargs = load.optimizer(args.optimizer)
    optimizer = opt_class(generator.parameters(model), lr=args.lr, weight_decay=args.weight_decay, **opt_kwargs)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=args.lr_drops, gamma=args.lr_drop_rate)


    ## Pre-Train ##
    print('Pre-Train for {} epochs.'.format(args.pre_epochs))
    pre_result = train_eval_loop(model, loss, optimizer, scheduler, train_loader, 
                                test_loader, device, args.pre_epochs, args.verbose)
    
    ## Save Original ##
    torch.save(model.state_dict(),"{}/pre_train_model.pt".format(args.result_dir))
    torch.save(optimizer.state_dict(),"{}/pre_train_optimizer.pt".format(args.result_dir))
    torch.save(scheduler.state_dict(),"{}/pre_train_scheduler.pt".format(args.result_dir))

    for compression in args.compression_list:
        for p in args.pruner_list:
            # Reset Model, Optimizer, and Scheduler
            print('compression ratio: {} ::: pruner: {}'.format(compression, p))
            model.load_state_dict(torch.load("{}/pre_train_model.pt".format(args.result_dir), map_location=device))
            optimizer.load_state_dict(torch.load("{}/pre_train_optimizer.pt".format(args.result_dir), map_location=device))
            scheduler.load_state_dict(torch.load("{}/pre_train_scheduler.pt".format(args.result_dir), map_location=device))

            ## Prune ##
            print('Pruning with {} for {} epochs.'.format(p, args.prune_epochs))
            pruner = load.pruner(p)(generator.masked_parameters(model, args.prune_bias, args.prune_batchnorm, args.prune_residual))
            sparsity = 10**(-float(compression))
            prune_loop(model, loss, pruner, prune_loader, device, sparsity, 
                    args.compression_schedule, args.mask_scope, args.prune_epochs, args.reinitialize, args.prune_train_mode, args.shuffle, args.invert)

            
            ## Post-Train ##
            print('Post-Training for {} epochs.'.format(args.post_epochs))
            post_result = train_eval_loop(model, loss, optimizer, scheduler, train_loader, 
                                        test_loader, device, args.post_epochs, args.verbose) 
            
            ## Display Results ##
            frames = [pre_result.head(1), pre_result.tail(1), post_result.head(1), post_result.tail(1)]
            train_result = pd.concat(frames, keys=['Init.', 'Pre-Prune', 'Post-Prune', 'Final'])
            prune_result = metrics.summary(model, 
                                        pruner.scores,
                                        metrics.flop(model, input_shape, device),
                                        lambda p: generator.prunable(p, args.prune_batchnorm, args.prune_residual))
            total_params = int((prune_result['sparsity'] * prune_result['size']).sum())
            possible_params = prune_result['size'].sum()
            total_flops = int((prune_result['sparsity'] * prune_result['flops']).sum())
            possible_flops = prune_result['flops'].sum()
            print("Train results:\n", train_result)
            print("Prune results:\n", prune_result)
            print("Parameter Sparsity: {}/{} ({:.4f})".format(total_params, possible_params, total_params / possible_params))
            print("FLOP Sparsity: {}/{} ({:.4f})".format(total_flops, possible_flops, total_flops / possible_flops))
            
            ## recording testing time for task 2 ##
            start_time = timeit.default_timer()
            # evaluating the model, including some data gathering overhead
            # eval(model, loss, test_loader, device, args.verbose)
            model.eval()
            with torch.no_grad():
                for data, target in test_loader:
                    data, target = data.to(device), target.to(device)
                    model(data)
                    break
            end_time = timeit.default_timer()
            print("Testing time: {:.4f}".format(end_time - start_time))

            fout.write('compression ratio: {} ::: pruner: {}'.format(compression, p))
            fout.write('Train results:\n {}\n'.format(train_result))
            fout.write('Prune results:\n {}\n'.format(prune_result))
            fout.write('Parameter Sparsity: {}/{} ({:.4f})\n'.format(total_params, possible_params, total_params / possible_params))
            fout.write("FLOP Sparsity: {}/{} ({:.4f})\n".format(total_flops, possible_flops, total_flops / possible_flops))
            fout.write("Testing time: {}\n".format(end_time - start_time))
            fout.write("remaining weights: \n{}\n".format((prune_result['sparsity'] * prune_result['size'])))
            fout.write('flop each layer: {}\n'.format((prune_result['sparsity'] * prune_result['flops']).values.tolist()))
            ## Save Results and Model ##
            if args.save:
                print('Saving results.')
                if not os.path.exists('{}/{}'.format(args.result_dir, compression)):
                    os.makedirs('{}/{}'.format(args.result_dir, compression))
                # pre_result.to_pickle("{}/{}/pre-train.pkl".format(args.result_dir, compression))
                # post_result.to_pickle("{}/{}/post-train.pkl".format(args.result_dir, compression))
                # prune_result.to_pickle("{}/{}/compression.pkl".format(args.result_dir, compression))
                # torch.save(model.state_dict(), "{}/{}/model.pt".format(args.result_dir, compression))
                # torch.save(optimizer.state_dict(),
                #         "{}/{}/optimizer.pt".format(args.result_dir, compression))
                # torch.save(scheduler.state_dict(),
                #         "{}/{}/scheduler.pt".format(args.result_dir, compression))

    fout.close()
